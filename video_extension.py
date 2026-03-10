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
import threading  # 🌟 新增：用于多线程状态锁
from openai import OpenAI
from flask import Blueprint, request, jsonify, Response, stream_with_context
from concurrent.futures import ThreadPoolExecutor, as_completed # 🌟 新增 as_completed

video_bp = Blueprint('video', __name__)
DB_PATH = 'universal_data.db'
RESOURCE_NODE_URL = "http://192.168.31.204:8100"

# 🌟 核心修复：更正为阿里支持的真实模型名称
DEFAULT_NLP_MODEL = 'qwen3.5-27b' 
# DEFAULT_NLP_MODEL = 'qwen-plus'  # 或 'qwen3.5-plus'，建议先用 qwen-plus 测试
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 🌟 初始化 OpenAI 客户端
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=DASHSCOPE_BASE_URL,
)

# 🌟 核心修改：定义线程池和全局锁
executor = ThreadPoolExecutor(max_workers=6)
state_lock = threading.Lock()

# ================= 🌟 AI 任务状态机 =================
# ================= 🌟 AI 任务状态机 =================
ai_scan_state = {
    "is_running": False,
    "total": 0,
    "processed": 0,       
    "success_count": 0,   
    "status_msg": "就绪，等待扫描",
    "total_time_sec": 0,
    "ai_model": DEFAULT_NLP_MODEL,
    "recent_results": []  # 🌟 新增：用于存放最新解析的5条结果
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

# 🌟 核心修改：分离出的单批次处理函数，供线程池并发调用
def process_single_batch(batch, batch_id):
    global ai_scan_state
    
    if not ai_scan_state["is_running"]:
        return

    prompt = f"""你是一个短视频内容分析引擎。请根据以下视频文件名，为每个视频推断出 1 个【主分类】(如: 影视, 搞笑, 学习, 颜值, 音乐, 随拍) 和 3 到 5 个【子标签】。
    务必返回纯JSON，格式：{{"video.mp4": {{"category": "影视", "tags": ["混剪", "动作"]}}}}
    文件名列表：{json.dumps(batch, ensure_ascii=False)}"""
    
    batch_start_time = time.time()
    try:
        completion = client.chat.completions.create(
            model=DEFAULT_NLP_MODEL,
            messages=[
                {'role': 'system', 'content': '你是一个只返回纯JSON的分析助手。'},
                {'role': 'user', 'content': prompt}
            ],
            response_format={ "type": "json_object" }
        )
        
        elapsed = round(time.time() - batch_start_time, 2)
        result_text = completion.choices[0].message.content.strip()
        
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            ai_data = json.loads(match.group(0))
            last_fname, last_tags_str = "", ""
            
            # 使用带超时设置的独立连接，避免多线程写入锁冲突
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    if not isinstance(tags, list): tags = [tags]
                    
                    conn.execute("""
                        INSERT OR REPLACE INTO video_store (filename, category, tags, ai_model, ai_time_sec) 
                        VALUES (?, ?, ?, ?, ?)
                    """, (fname, info.get('category', '未分类'), json.dumps(tags[:5], ensure_ascii=False), DEFAULT_NLP_MODEL, elapsed))
                    
                    last_fname, last_tags_str = fname, " ".join([f"#{t}" for t in tags[:3]])
            
           # 使用锁安全地更新全局状态
            with state_lock:
                ai_scan_state["success_count"] += len(ai_data)
                ai_scan_state["processed"] += len(batch)
                ai_scan_state["status_msg"] = f"✅ {last_fname[:20]}... {last_tags_str}"
                
                # 🌟 新增：把当前批次的结果推入展示队列（头部插入），只保留最新的5个
                for fname, info in ai_data.items():
                    tags = info.get('tags', [])
                    if not isinstance(tags, list): tags = [tags]
                    ai_scan_state["recent_results"].insert(0, {
                        "filename": fname,
                        "category": info.get('category', '未分类'),
                        "ai_tags": tags[:5]
                    })
                # 截断队列，防止内存溢出
                ai_scan_state["recent_results"] = ai_scan_state["recent_results"][:5]
            
    except Exception as e:
        elapsed = round(time.time() - batch_start_time, 2)
        err_msg = str(e)
        with state_lock:
            ai_scan_state["processed"] += len(batch)
            ai_scan_state["status_msg"] = f"❌ API报错: {err_msg[:40]}"
        if "RateLimit" in err_msg: time.sleep(5)

# 🌟 核心修改：任务调度主函数
def ai_tag_videos_task():
    global ai_scan_state
    
    ai_scan_state.update({
        "is_running": True, "total": 0, "processed": 0, 
        "success_count": 0, "status_msg": "正在同步列表...", 
        "total_time_sec": 0, "ai_model": DEFAULT_NLP_MODEL,
        "recent_results": [] # 🌟 每次启动任务清空历史展示
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
    # 拆分所有批次
    batches = [untagged[i:i+batch_size] for i in range(0, len(untagged), batch_size)]
    
    futures = []
    # 🌟 提交所有批次到线程池并发执行
    for idx, batch in enumerate(batches):
        if not ai_scan_state["is_running"]:
            break
        futures.append(executor.submit(process_single_batch, batch, idx + 1))
        # 轻微休眠，防止瞬间发出大量请求导致 QPS 限制
        time.sleep(0.5)
        
        # 定期更新总耗时
        with state_lock:
             ai_scan_state["total_time_sec"] = round(time.time() - task_start_time, 1)

    # 等待所有提交的线程完成
    for future in as_completed(futures):
         with state_lock:
             ai_scan_state["total_time_sec"] = round(time.time() - task_start_time, 1)
            
    if ai_scan_state["is_running"]:
        ai_scan_state["is_running"] = False
        ai_scan_state["status_msg"] = f"🎉 成功打标 {ai_scan_state['success_count']} 个视频！"


# ================= 接口：控制与统计 (保持原逻辑) =================
@video_bp.route('/api/video/scan', methods=['POST'])
def control_scan():
    global ai_scan_state
    req_data = request.get_json(silent=True) or {}
    if req_data.get('action') == 'start' and not ai_scan_state["is_running"]:
        # 🌟 核心修改：使用独立线程启动主调度函数，防止阻塞 Flask 主线程
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
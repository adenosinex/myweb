# tags_extension.py
import sqlite3
import json
import csv
import io
import os
import re
import requests
import threading
import queue
import time
from flask import Blueprint, request, jsonify, make_response

tags_bp = Blueprint('tags', __name__)
DB_PATH = 'db/universal_data.db'

# ================= 配置管理模块 =================
TAG_CHOICES = [
    "深夜emo", "健身", "说唱", "图书馆", "通勤", "驾车",
    "起床", "DJ", "助眠", "抖音漫游", "Chill", "洗澡",
    "快乐", "电音", "音乐视频", "春节", "粤语", "失恋",
    "躺平", "欧美", "打扫", "国风", "会员", "游戏",
    "专注", "沉浸", "夜晚", "治愈", "轻音乐", "小酒馆",
    "KTV", "情歌", "摇滚", "R&B", "佛系", "怀旧",
    "民谣", "女声", "K-pop", "日语", "旅行", "摸鱼",
    "儿歌", "雨天", "海边", "乡村", "古典", "学习"
]

@tags_bp.route('/api/tags', methods=['GET'])
def get_tags():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS song_tags (
                song_name TEXT PRIMARY KEY,
                tags TEXT
            )
        ''')
        cursor.execute('SELECT song_name, tags FROM song_tags')
        rows = cursor.fetchall()
    
    tags_dict = {}
    all_categories = set()
    for row in rows:
        try:
            tags = json.loads(row[1])
            tags_dict[row[0]] = tags
            all_categories.update(tags)
        except json.JSONDecodeError:
            tags_dict[row[0]] = []
            
    return jsonify({
        "song_tags": tags_dict,
        "categories": list(all_categories)
    })

@tags_bp.route('/api/tags', methods=['POST'])
def save_tags():
    data = request.json
    song_name = data.get('song_name')
    tags = data.get('tags', [])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR REPLACE INTO song_tags (song_name, tags) VALUES (?, ?)',
                     (song_name, json.dumps(tags, ensure_ascii=False)))
    return jsonify({"status": "success"})

@tags_bp.route('/api/tags', methods=['DELETE'])
def delete_tags():
    song_name = request.json.get('song_name')
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM song_tags WHERE song_name=?', (song_name,))
    return jsonify({"status": "success"})

@tags_bp.route('/api/tags/csv', methods=['GET'])
def download_tags_csv():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT song_name, tags FROM song_tags')
        rows = cursor.fetchall()
    
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['歌曲名', '分类标签'])
    for row in rows:
        tags_str = " / ".join(json.loads(row[1]))
        cw.writerow([row[0], tags_str])
        
    output = make_response(si.getvalue().encode('utf-8-sig'))
    output.headers["Content-Disposition"] = "attachment; filename=song_tags_export.csv"
    output.headers["Content-type"] = "text/csv; charset=utf-8"
    return output

# ================= AI 动态批处理核心 =================

BATCH_QUEUE = queue.Queue()
MAX_BATCH_SIZE = 5      # 每次最多打包 5 首
BATCH_TIMEOUT = 1.0     # 最多等待 1 秒
def call_silicon_batch(song_names, api_key, model_name):
    """
    底层发包函数：将多首歌一并发送给大模型，并要求强制返回严格的 JSON 对象
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    tags_str = "，".join(TAG_CHOICES)
    
    prompt = f"""
请为以下列表中的每首歌曲，从给定的标签库中选择1到3个最符合其受众或氛围的标签。
标签库：[{tags_str}]

歌曲列表：
{json.dumps(song_names, ensure_ascii=False)}

【极度重要约束】：
1. 你的返回内容必须是纯粹的 JSON 格式（不要加 ```json 代码块，直接输出大括号）。
2. JSON 的键必须与列表中提供的歌曲名一字不差！
3. 值必须是一个字符串数组，里面的标签必须且只能来自上述标签库。

返回示例：
{{
  "歌曲A": ["流行", "快乐"],
  "歌曲B": ["深夜emo", "伤感"]
}}
"""
    payload = {
        "model": model_name, 
        "messages": [
            {"role": "system", "content": "你是一个严格遵循 JSON 格式输出协议的音乐分类专家。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    try:
        # 发送请求
        resp = requests.post("https://api.siliconflow.cn/v1/chat/completions", json=payload, headers=headers, timeout=20)
        
        # 1. 拦截所有非 200 的状态码 (比如 429, 502 等)
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
            
        # 2. 安全解析 JSON
        try:
            res_json = resp.json()
        except Exception:
            raise Exception(f"API 返回了非 JSON 数据: {resp.text[:200]}")
            
        if 'choices' not in res_json:
            error_msg = res_json.get('error', {}).get('message', str(res_json))
            raise Exception(f"API 服务端拒绝: {error_msg}")
            
        raw_text = res_json['choices'][0]['message']['content'].strip()
        
        # 3. 尝试暴力提取 JSON，防大模型返回多余文本
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if not match:
            raise Exception(f"大模型未能返回合法的 JSON 结构。原始输出: {raw_text[:100]}...")
            
        try:
            result_dict = json.loads(match.group(0))
        except Exception as e:
            raise Exception(f"大模型返回的 JSON 格式损坏: {e}。原始输出: {match.group(0)[:100]}")
        
        # 清洗：过滤掉不在 TAG_CHOICES 里的幻觉标签
        clean_result = {}
        for song, tags in result_dict.items():
            if isinstance(tags, list):
                valid_tags = [t for t in tags if t in TAG_CHOICES][:3]
                clean_result[song] = valid_tags if valid_tags else ["未分类"]
            else:
                clean_result[song] = ["未分类"]
                
        return clean_result

    except requests.exceptions.RequestException as e:
        raise Exception(f"网络连接层异常: {e}")

def ai_batch_worker():
    """后台工作线程：负责收割队列，批量请求大模型"""
    print("[AI Worker] 动态批处理引擎已启动")
    while True:
        batch_items = []
        # 1. 阻塞等待第一个请求到达
        first_item = BATCH_QUEUE.get()
        batch_items.append(first_item)
        
        # 2. 开启 1 秒窗口期，收集后续请求
        start_time = time.time()
        while len(batch_items) < MAX_BATCH_SIZE:
            elapsed = time.time() - start_time
            if elapsed >= BATCH_TIMEOUT:
                break
            try:
                item = BATCH_QUEUE.get(timeout=BATCH_TIMEOUT - elapsed)
                batch_items.append(item)
            except queue.Empty:
                break
                
        song_names = [item['song_name'] for item in batch_items]
        api_key = batch_items[0]['api_key']
        model_name = batch_items[0]['model_name']
        
        print(f"[AI Worker] 聚合发车: 共 {len(batch_items)} 首 -> {song_names}")
        
        try:
            # 3. 真正向外发送网络请求
            result_dict = call_silicon_batch(song_names, api_key, model_name)
            
            # 4. 精确分发结果并唤醒各个阻塞的请求线程
            for item in batch_items:
                song = item['song_name']
                # 用 get 方法获取，防止大模型遗漏了某首歌
                item['result'] = result_dict.get(song, ["未分类"])
                item['event'].set()
                
        except Exception as e:
            print(f"[AI Worker Error] 批量处理崩溃: {e}")
            for item in batch_items:
                item['error'] = str(e)
                item['event'].set()

# 启动线程
# 启动多条后台收割机线程（建立并发池）
WORKER_COUNT = 5  # 你可以根据 API 的限流承受能力调整这个数字
for _ in range(WORKER_COUNT):
    threading.Thread(target=ai_batch_worker, daemon=True).start()
print(f"🚀 成功启动 {WORKER_COUNT} 条 AI 动态批处理流水线！")

# --- 接收端路由 ---
@tags_bp.route('/api/ai/tag', methods=['POST'])
def tag_song_api():
    """
    前端每首歌依然单独请求这个接口，但这只是一个“挂号处”。
    真正的看病逻辑在后台 worker 线程里，按批次处理。
    """
    data = request.json
    song_name = data.get('song_name')
    
    api_key = data.get('api_key') or os.environ.get('SILICONFLOW_API_KEY')
    model_name = data.get('model_name') or os.environ.get('SILICON_MODEL')
    
    if not api_key:
        return jsonify({"error": "缺少 API Key"}), 400
        
    event = threading.Event()
    req_context = {
        'song_name': song_name,
        'api_key': api_key,
        'model_name': model_name,
        'event': event,
        'result': None,
        'error': None
    }
    
    # 丢进公交车站，等车来
    BATCH_QUEUE.put(req_context)
    
    # 本请求线程挂起，最多等 25 秒 (1秒等车 + 20秒大模型处理 + 缓冲)
    success = event.wait(timeout=25.0)
    
    if not success:
        return jsonify({"error": "队列等待或模型响应超时"}), 504
    if req_context['error']:
        return jsonify({"error": req_context['error']}), 500
        
    return jsonify({"tags": req_context['result']})
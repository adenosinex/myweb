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

# ================= 环境变量全局加载与验证 =================
GLOBAL_MODEL_MUSIC_API = os.environ.get('MODEL_MUSIC_API', '').strip()
GLOBAL_MODEL_MUSIC = os.environ.get('MODEL_MUSIC', '').strip()
GLOBAL_MODEL_MUSIC_URL = os.environ.get('MODEL_MUSIC_URL', '').strip()

# print("="*50)
# print("🎵 AI Music Tags - 环境变量加载检查")
# if GLOBAL_MODEL_MUSIC_API:
#     print(f"[*] MODEL_MUSIC_API: {GLOBAL_MODEL_MUSIC_API[:8]}... (长度: {len(GLOBAL_MODEL_MUSIC_API)})")
# else:
#     print("[!] MODEL_MUSIC_API: 未设置或为空!")
# print(f"[*] MODEL_MUSIC    : {GLOBAL_MODEL_MUSIC if GLOBAL_MODEL_MUSIC else '未设置或为空!'}")
# print(f"[*] MODEL_MUSIC_URL: {GLOBAL_MODEL_MUSIC_URL if GLOBAL_MODEL_MUSIC_URL else '未设置或为空!'}")
# print("="*50)


# ================= 配置管理模块 =================
TAG_CHOICES = [
    # 曲风/风格
    "流行", "摇滚", "民谣", "电子", "说唱", "R&B", "古典", "爵士", "古风", "国风",
    "乡村", "蓝调", "金属", "朋克", "雷鬼", "放克", "灵魂乐",

    # 情绪/氛围
    "快乐", "伤感", "浪漫", "孤独", "甜蜜", "安静", "放松", "激昂", "治愈", "忧郁",
    "温暖", "紧张", "神秘",

    # 节奏/速度
    "快节奏", "中速", "慢节奏", "舒缓", "节奏感强",

    # 演唱形式
    "男声", "女声", "合唱", "独唱", "说唱", "戏腔", "纯音乐",

    # 编曲特征
    "钢琴", "吉他", "弦乐", "电子合成器", "鼓点重", "人声为主",
]

# ================= 新增：遥测数据记录函数 =================
def log_telemetry(model_name, batch_size, telemetry_data, total_time, status):
    """记录每次大模型调用的性能指标"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS model_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                model_name TEXT,
                batch_size INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                pure_ai_time REAL,
                total_time REAL,
                status TEXT
            )
        ''')
        conn.execute('''
            INSERT INTO model_telemetry 
            (model_name, batch_size, prompt_tokens, completion_tokens, total_tokens, pure_ai_time, total_time, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            model_name,
            batch_size,
            telemetry_data.get('prompt_tokens', 0),
            telemetry_data.get('completion_tokens', 0),
            telemetry_data.get('total_tokens', 0),
            telemetry_data.get('pure_ai_time', 0.0),
            round(total_time, 3),
            status
        ))
 

@tags_bp.route('/api/tags', methods=['GET'])
def get_tags():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS song_tags (
                song_name TEXT PRIMARY KEY,
                tags TEXT,
                model_name TEXT,
                time_taken REAL
            )
        ''')
        # 兼容旧数据库结构，自动追加新字段
        try:
            cursor.execute('ALTER TABLE song_tags ADD COLUMN model_name TEXT')
            cursor.execute('ALTER TABLE song_tags ADD COLUMN time_taken REAL')
        except sqlite3.OperationalError:
            pass # 字段已存在

        cursor.execute('SELECT song_name, tags, model_name, time_taken FROM song_tags')
        rows = cursor.fetchall()
    
    tags_dict = {}
    all_categories = set()
    for row in rows:
        try:
            tags = json.loads(row[1])
            # 返回数据中附加模型和耗时信息
            tags_dict[row[0]] = {
                "tags": tags,
                "model_name": row[2] if row[2] else "",
                "time_taken": row[3] if row[3] else 0.0
            }
            all_categories.update(tags)
        except json.JSONDecodeError:
            tags_dict[row[0]] = {"tags": [], "model_name": "", "time_taken": 0.0}
            
    return jsonify({
        "song_tags": tags_dict,
        "categories": list(all_categories)
    })

@tags_bp.route('/api/tags', methods=['POST'])
def save_tags():
    data = request.json
    song_name = data.get('song_name')
    tags = data.get('tags', [])
    model_name = data.get('model_name', '')
    time_taken = data.get('time_taken', 0.0)
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR REPLACE INTO song_tags (song_name, tags, model_name, time_taken) VALUES (?, ?, ?, ?)',
                     (song_name, json.dumps(tags, ensure_ascii=False), model_name, time_taken))
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
        
        # 确保遥测子表存在，防止在未发生过任何请求前直接导出引发 SQL 报错
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS model_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                model_name TEXT,
                batch_size INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                pure_ai_time REAL,
                total_time REAL,
                status TEXT
            )
        ''')
        
        # 联合查询：左连接主表与子表的聚合数据
        query = '''
            SELECT 
                st.song_name, 
                st.tags, 
                st.model_name, 
                st.time_taken,
                mt.avg_pure_ai,
                mt.avg_total_tokens
            FROM song_tags st
            LEFT JOIN (
                SELECT 
                    model_name, 
                    ROUND(AVG(pure_ai_time), 3) AS avg_pure_ai,
                    ROUND(AVG(total_tokens), 1) AS avg_total_tokens
                FROM model_telemetry
                WHERE status = 'success'
                GROUP BY model_name
            ) mt ON st.model_name = mt.model_name
        '''
        cursor.execute(query)
        rows = cursor.fetchall()
    
    si = io.StringIO()
    cw = csv.writer(si)
    # 更新表头以匹配新增的详细联合信息
    cw.writerow([
        '歌曲名', 
        '分类标签', 
        '识别模型', 
        '单首分摊耗时(s)', 
        '模型批次均次推理耗时(s)', 
        '模型批次均消耗Tokens'
    ])
    
    for row in rows:
        song_name = row[0]
        try:
            tags_str = " / ".join(json.loads(row[1])) if row[1] else "未分类"
        except json.JSONDecodeError:
            tags_str = "数据损坏"
            
        model_name = row[2] if row[2] else "未知"
        time_taken = row[3] if row[3] else 0.0
        avg_pure_ai = row[4] if row[4] is not None else "N/A"
        avg_total_tokens = row[5] if row[5] is not None else "N/A"
        
        cw.writerow([
            song_name, 
            tags_str, 
            model_name, 
            time_taken, 
            avg_pure_ai, 
            avg_total_tokens
        ])
        
    output = make_response(si.getvalue().encode('utf-8-sig'))
    # 修改了默认文件名以区分旧版基础导出
    output.headers["Content-Disposition"] = "attachment; filename=song_tags_detailed_export.csv"
    output.headers["Content-type"] = "text/csv; charset=utf-8"
    return output

@tags_bp.route('/api/tags/csv_import', methods=['POST'])
def import_and_reidentify_csv():
    """
    接收上传的 CSV 文件，提取歌曲名并批量重新调用大模型获取 Tag。
    识别成功后自动更新数据库。
    """
    if 'file' not in request.files:
        return jsonify({"error": "未检测到文件上传"}), 400
        
    file = request.files['file']
    # 引用全局变量
    api_key = request.form.get('api_key') or GLOBAL_MODEL_MUSIC_API
    model_name = request.form.get('model_name') or GLOBAL_MODEL_MUSIC
    
    if not api_key:
        return jsonify({"error": "缺少 API Key"}), 400

    try:
        stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        csv_input = csv.reader(stream)
        headers = next(csv_input, None)
        
        # 定位“歌曲名”列索引，若无表头默认取第0列
        song_idx = 0
        if headers and '歌曲名' in headers:
            song_idx = headers.index('歌曲名')
            
        songs_to_process = []
        for row in csv_input:
            if row and len(row) > song_idx and row[song_idx].strip():
                songs_to_process.append(row[song_idx].strip())
                
    except Exception as e:
        return jsonify({"error": f"CSV解析失败: {str(e)}"}), 400

    success_count = 0
    fail_count = 0

    # 为了避免通过Queue阻塞导致HTTP请求超时，此处对CSV数据直接切片并调用底层发包函数
    for i in range(0, len(songs_to_process), MAX_BATCH_SIZE):
        chunk = songs_to_process[i:i + MAX_BATCH_SIZE]
        start_time = time.time()
        
        try:
            result_dict, telemetry_data = call_silicon_batch(chunk, api_key, model_name)
            batch_time = time.time() - start_time
            avg_time = batch_time / len(chunk)
            
            # 批量写入数据库
            with sqlite3.connect(DB_PATH) as conn:
                for song in chunk:
                    tags = result_dict.get(song, ["未分类"])
                    conn.execute(
                        'INSERT OR REPLACE INTO song_tags (song_name, tags, model_name, time_taken) VALUES (?, ?, ?, ?)',
                        (song, json.dumps(tags, ensure_ascii=False), model_name, round(avg_time, 3))
                    )
                    success_count += 1
            
            # 记录成功遥测数据
            log_telemetry(model_name, len(chunk), telemetry_data, batch_time, "success")
            
        except Exception as e:
            batch_time = time.time() - start_time
            print(f"[CSV Import Error] 批处理失败 (歌曲: {chunk}): {e}")
            fail_count += len(chunk)
            # 记录失败遥测数据
            log_telemetry(model_name, len(chunk), {}, batch_time, f"error: {str(e)[:50]}")

    return jsonify({
        "status": "completed", 
        "success_count": success_count, 
        "fail_count": fail_count,
        "total": len(songs_to_process)
    })


# ================= AI 动态批处理核心 =================

BATCH_QUEUE = queue.Queue()
MAX_BATCH_SIZE = 10      # 每次最多打包 5 首
BATCH_TIMEOUT = 3.0     # 最多等待 1 秒

def call_silicon_batch(song_names, api_key, model_name):
    """
    底层发包函数：将多首歌一并发送给大模型，并要求强制返回严格的 JSON 对象
    返回: (clean_result_dict, telemetry_data)
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
        # 增加对 URL 空值的校验，防止 requests 发起非法请求
        if not GLOBAL_MODEL_MUSIC_URL:
            raise Exception("服务端未配置环境变量 MODEL_MUSIC_URL 或其为空")

        # 记录纯 AI 推理耗时
        ai_start_time = time.time()
        resp = requests.post(GLOBAL_MODEL_MUSIC_URL, json=payload, headers=headers, timeout=120)
        pure_ai_time = time.time() - ai_start_time
        
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
        
        # 提取 Token 使用量
        usage = res_json.get('usage', {})
        telemetry_data = {
            'prompt_tokens': usage.get('prompt_tokens', 0),
            'completion_tokens': usage.get('completion_tokens', 0),
            'total_tokens': usage.get('total_tokens', 0),
            'pure_ai_time': pure_ai_time
        }
        
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
                
        return clean_result, telemetry_data

    except requests.exceptions.RequestException as e:
        raise Exception(f"网络连接层异常: {e}")

def ai_batch_worker():
    """后台工作线程：负责收割队列，批量请求大模型"""
    # print("[AI Worker] 动态批处理引擎已启动")
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
        batch_size = len(batch_items)
        
        print(f"[AI Worker] 聚合发车: 共 {batch_size} 首 -> {song_names}")
        
        req_start_time = time.time()
        try:
            # 3. 真正向外发送网络请求并记录时间
            result_dict, telemetry_data = call_silicon_batch(song_names, api_key, model_name)
            total_time = time.time() - req_start_time
            avg_time = total_time / batch_size  # 计算单首平均耗时
            
            # 记录成功遥测数据
            log_telemetry(model_name, batch_size, telemetry_data, total_time, "success")
            
            # 4. 精确分发结果并唤醒各个阻塞的请求线程
            for item in batch_items:
                song = item['song_name']
                # 用 get 方法获取，防止大模型遗漏了某首歌
                item['result'] = result_dict.get(song, ["未分类"])
                item['time_taken'] = avg_time
                item['event'].set()
                
        except Exception as e:
            total_time = time.time() - req_start_time
            print(f"[AI Worker Error] 批量处理崩溃: {e}")
            
            # 记录失败遥测数据
            log_telemetry(model_name, batch_size, {}, total_time, f"error: {str(e)[:50]}")
            
            for item in batch_items:
                item['error'] = str(e)
                item['event'].set()

# 启动线程
# 启动多条后台收割机线程（建立并发池）
WORKER_COUNT = 2  # 你可以根据 API 的限流承受能力调整这个数字
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
    
    # 引用全局变量
    api_key = data.get('api_key') or GLOBAL_MODEL_MUSIC_API
    model_name = data.get('model_name') or GLOBAL_MODEL_MUSIC
    
    if not api_key:
        return jsonify({"error": "缺少 API Key"}), 400
        
    event = threading.Event()
    req_context = {
        'song_name': song_name,
        'api_key': api_key,
        'model_name': model_name,
        'event': event,
        'result': None,
        'time_taken': 0.0,
        'error': None
    }
    
    # 丢进公交车站，等车来
    BATCH_QUEUE.put(req_context)
    
    # 本请求线程挂起，最多等 125 秒
    success = event.wait(timeout=125.0)
    
    if not success:
        return jsonify({"error": "队列等待或模型响应超时"}), 504
    if req_context['error']:
        return jsonify({"error": req_context['error']}), 500
        
    return jsonify({
        "tags": req_context['result'],
        "model_name": req_context['model_name'],
        "time_taken": round(req_context['time_taken'], 3)
    })
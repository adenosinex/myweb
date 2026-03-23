import os
import sqlite3
import json
import uuid
import time
import base64
import requests
from flask import Blueprint, request, jsonify, send_file, abort
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

snapshots_bp = Blueprint('snapshots', __name__)
DB_PATH = 'db/universal_data.db' 
UPLOAD_FOLDER = 'db/uploads/snapshots'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

executor = ThreadPoolExecutor(max_workers=5)

# ================= 模型接口配置 =================
VL_CONFIG = {
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "api_key": os.getenv("SILICONFLOW_API_KEY", ""), # 即使为空也不影响核心记录功能
    "model": "Qwen/Qwen2.5-VL-32B-Instruct"
}

def call_vl_api(config, messages):
    if not config['api_key']:
        raise Exception("未配置 API Key，无法使用 AI 功能。但基础图片记录功能不受影响。")
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}"
    }
    payload = {
        "model": config['model'],
        "messages": messages
    }
    
    response = requests.post(config['base_url'], headers=headers, json=payload, timeout=120)
    if response.status_code != 200:
        raise Exception(f"API Error ({response.status_code}): {response.text}")
        
    return response.json()['choices'][0]['message']['content']

# ================= 数据库初始化 =================
def init_snapshots_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY,
                event_type TEXT,        -- 前端传入的自定义动作，如：剪指甲
                filepath TEXT,
                bg_description TEXT,    -- AI 生成或手动记录的背景描述
                tags TEXT,              -- JSON 数组
                ai_model TEXT,
                processing_time_sec REAL,
                status TEXT,
                record_time DATETIME DEFAULT (datetime('now', 'localtime')),
                created_at DATETIME DEFAULT (datetime('now', 'localtime'))
            )
        ''')
init_snapshots_db()

def update_status(record_id, status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE snapshots SET status = ? WHERE id = ?", (status, record_id))

# ================= 按需触发的 AI 分析逻辑 =================

def process_vision_task(record_id, absolute_filepath):
    """仅在用户主动请求时执行，提取环境信息并更新对应记录"""
    start_time = time.time()
    try:
        update_status(record_id, 'analyzing_context')
        
        with open(absolute_filepath, 'rb') as f:
            base64_img = base64.b64encode(f.read()).decode('utf-8')
        img_data_uri = f"data:image/jpeg;base64,{base64_img}"

        prompt = """
        这是一张个人生活周期打卡照片。请重点观察【背景环境】以记录当前的生活切片。
        请只返回严格的JSON格式数据，不要包含任何其他解释性文字。必须包含以下字段：
        {
            "event_type": "画面核心的动作或主体",
            "bg_description": "详细描述背景中的环境和物品状态（这对于未来回溯时光具有核心价值）",
            "tags": ["场景标签1", "场景标签2"]
        }
        """
        vl_messages = [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": img_data_uri}}]}]
        
        raw_output = call_vl_api(VL_CONFIG, vl_messages)
        
        try:
            # 不使用正则，通过字符串截取清理 Markdown 标记
            json_str = raw_output.strip()
            if json_str.startswith("```json"):
                json_str = json_str[7:]
            elif json_str.startswith("```"):
                json_str = json_str[3:]
            
            if json_str.endswith("```"):
                json_str = json_str[:-3]
                
            json_str = json_str.strip()
            
            result_data = json.loads(json_str)
            ai_event_type = result_data.get("event_type", "")
            bg_desc = result_data.get("bg_description", "")
            tags = result_data.get("tags", [])
        except json.JSONDecodeError as e:
            bg_desc = f"JSON解析失败: {str(e)}\n\n原始输出:\n{raw_output}"
            tags = ["AI解析异常"]
            ai_event_type = ""

        duration = round(time.time() - start_time, 2)
        
        with sqlite3.connect(DB_PATH) as conn:
            # 如果原记录没有 event_type，则使用 AI 识别的补充进去
            cursor = conn.cursor()
            cursor.execute("SELECT event_type FROM snapshots WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            current_event = row[0] if row else "未命名记录"
            
            final_event = current_event if current_event and current_event != "未命名记录" else ai_event_type

            conn.execute('''
                UPDATE snapshots 
                SET event_type = ?, bg_description = ?, tags = ?, 
                    processing_time_sec = ?, ai_model = ?, status = 'completed'
                WHERE id = ?
            ''', (final_event, bg_desc, json.dumps(tags, ensure_ascii=False), duration, VL_CONFIG['model'], record_id))
            
    except Exception as e:
        with sqlite3.connect(DB_PATH) as conn:
            # 失败后恢复 completed 状态，把错误信息写进描述里，不影响图片在前端的正常展示
            conn.execute("UPDATE snapshots SET status = 'completed', bg_description = ? WHERE id = ?", (f"AI 分析失败: {str(e)}", record_id))

# ================= 基础 CRUD 路由 (完全独立于 AI) =================

@snapshots_bp.route('/api/snapshots/upload', methods=['POST'])
def upload_snapshot():
    """纯粹的文件落地接口，不包含任何 AI 逻辑"""
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    
    event_type = request.form.get('event_type', '').strip() or '未命名记录'
    record_time = request.form.get('record_time')
    
    record_id = str(uuid.uuid4())
    filepath = os.path.join(UPLOAD_FOLDER, f"{record_id}.jpg")
    file.save(filepath)
    
    with sqlite3.connect(DB_PATH) as conn:
        if record_time:
            conn.execute('INSERT INTO snapshots (id, event_type, filepath, bg_description, tags, status, record_time) VALUES (?, ?, ?, ?, ?, ?, ?)', 
                         (record_id, event_type, filepath, "", "[]", "completed", record_time))
        else:
            conn.execute('INSERT INTO snapshots (id, event_type, filepath, bg_description, tags, status) VALUES (?, ?, ?, ?, ?, ?)', 
                         (record_id, event_type, filepath, "", "[]", "completed"))
    
    return jsonify({"status": "success", "id": record_id})

@snapshots_bp.route('/api/snapshots/analyze', methods=['POST'])
def trigger_ai_analysis():
    """按需触发的 AI 分析接口"""
    record_id = request.json.get('id')
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE snapshots SET status = 'analyzing_context' WHERE id = ?", (record_id,))
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM snapshots WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row: 
            executor.submit(process_vision_task, record_id, os.path.abspath(row[0]))
    return jsonify({"status": "started"})

@snapshots_bp.route('/api/img/snapshots/<imgname>', methods=['GET'])
def get_snapshot_image(imgname):
    img_path = Path(UPLOAD_FOLDER) / imgname
    if img_path.is_file(): return send_file(img_path)
    return abort(404, description="Image not found")

@snapshots_bp.route('/api/snapshots', methods=['GET'])
def get_snapshots():
    query = "SELECT id, event_type, record_time, tags, status, bg_description FROM snapshots ORDER BY record_time DESC"
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        
    return jsonify([{
        "id": r[0], "event_type": r[1], "record_time": r[2], 
        "url": f"/api/img/snapshots/{r[0]}.jpg",
        "tags": json.loads(r[3]) if r[3] else [],
        "status": r[4], "description": r[5]
    } for r in rows])

@snapshots_bp.route('/api/snapshots/<record_id>', methods=['DELETE'])
def delete_snapshot(record_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM snapshots WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            try: os.remove(row[0]) 
            except: pass
        conn.execute("DELETE FROM snapshots WHERE id = ?", (record_id,))
    return jsonify({"status": "success"})
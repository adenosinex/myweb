import os
import sqlite3
import json
import uuid
import time
import base64
import re
import requests
from flask import Blueprint, request, jsonify
from concurrent.futures import ThreadPoolExecutor

manuals_bp = Blueprint('manuals', __name__)
DB_PATH = 'universal_data.db' 
UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

executor = ThreadPoolExecutor(max_workers=3)

# ================= 模型接口配置 =================
# 可自由组合不同提供商（如 Dashscope, Ollama, OpenAI）
# 阿里
# OCR_CONFIG = {
#      "base_url":  "https://dashscope.aliyuncs.com/compatible-mode/v1",
#     "api_key": os.getenv(  "DASHSCOPE_API_KEY"),
#     "model": "qwen-vl-ocr-2025-11-20"
# }
# 硅基
OCR_CONFIG = {
    "base_url":  "https://api.siliconflow.cn/v1/chat/completions",
    "api_key": os.getenv(  "SILICONFLOW_API_KEY"),
    "model": "Qwen/Qwen2.5-VL-32B-Instruct"
}
# 超算
# NLP_CONFIG = {
#     "base_url":  "https://api.scnet.cn/api/llm/v1/ocr/recognize" ,
#     "api_key": os.getenv(  "CS_API_KEY"),
#     "model": "Qwen3-30B-A3B-Instruct-2507"
    
# }

NLP_CONFIG = {
      "base_url":  "https://api.siliconflow.cn/v1/chat/completions",
    "api_key": os.getenv(  "SILICONFLOW_API_KEY"),
    "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B"
    # "model": "Qwen/Qwen2.5-VL-32B-Instruct"
    
}
# NLP_CONFIG = {
#     "base_url": os.getenv("NLP_BASE_URL", "http://localhost:11434/v1/chat/completions"), # 示例：使用本地 Ollama
#     "api_key": os.getenv("NLP_API_KEY", "ollama_placeholder_key"), # Ollama 不需要真实 key，但需保持字段存在
#     "model": "qwen-plus" # 如果用 Ollama，可改为 'llama3', 'qwen2' 等
# }

# ================= 核心请求方法 =================
def call_openai_compatible_api(config, messages):
    """统一调用 OpenAI 兼容接口"""
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
def init_manuals_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS manuals (
                id TEXT PRIMARY KEY,
                filename TEXT,
                filepath TEXT,
                ocr_text TEXT,
                tags TEXT,
                ocr_model TEXT,
                nlp_model TEXT,
                processing_time_sec REAL,
                status TEXT,
                created_at DATETIME DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        for col in ['ocr_time_sec', 'nlp_time_sec', 'original_hash']:
            try: conn.execute(f'ALTER TABLE manuals ADD COLUMN {col} TEXT')
            except: pass
init_manuals_db()

def update_status(record_id, status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE manuals SET status = ? WHERE id = ?", (status, record_id))

def process_manual_task(record_id, absolute_filepath):
    start_time = time.time()
    try:
        # 1. OCR 阶段
        update_status(record_id, 'processing_ocr')
        
        with open(absolute_filepath, 'rb') as f:
            base64_img = base64.b64encode(f.read()).decode('utf-8')
        img_data_uri = f"data:image/jpeg;base64,{base64_img}"

        # 使用标准 OpenAI Vision 消息格式
        vl_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请提取图中所有文字，严禁遗漏。使用Markdown格式呈现表格和标题，保持原有阅读顺序。"},
                    {"type": "image_url", "image_url": {"url": img_data_uri}}
                ]
            }
        ]
        
        ocr_text_output = call_openai_compatible_api(OCR_CONFIG, vl_messages)
        # 兼容部分模型返回格式差异，确保提取出纯文本
        ocr_text = ocr_text_output if isinstance(ocr_text_output, str) else str(ocr_text_output)
        
        ocr_duration = round(time.time() - start_time, 2)
        
        # 2. NLP 阶段
        update_status(record_id, 'processing_nlp')
        
        nlp_messages = [
            {
                "role": "user", 
                "content": f"请提取2-5个凝练标签（如物品类、品牌、场景）。返回纯JSON数组，不要代码块。\n文本：{ocr_text[:2500]}"
            }
        ]
        
        raw_tags = call_openai_compatible_api(NLP_CONFIG, nlp_messages).strip()
        
        match = re.search(r'\[.*\]', raw_tags, re.DOTALL)
        tags_list = json.loads(match.group(0)) if match else ["未分类"]
        
        total_duration = round(time.time() - start_time, 2)

        # 3. 存储结果
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                UPDATE manuals 
                SET ocr_text = ?, tags = ?, processing_time_sec = ?, ocr_time_sec = ?, nlp_time_sec = ?, status = 'completed',
                    ocr_model = ?, nlp_model = ?
                WHERE id = ?
            ''', (ocr_text, json.dumps(tags_list, ensure_ascii=False), total_duration, ocr_duration, total_duration - ocr_duration, 
                  OCR_CONFIG['model'], NLP_CONFIG['model'], record_id))
            
    except Exception as e:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE manuals SET status = 'failed', ocr_text = ? WHERE id = ?", (f"Error: {str(e)}", record_id))

# ================= 接口路由 =================
@manuals_bp.route('/api/manuals/upload', methods=['POST'])
def upload_manual():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    
    original_hash = request.form.get('original_hash')
    original_filename = request.form.get('original_filename', file.filename)
    
    if original_hash:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM manuals WHERE original_hash = ?", (original_hash,))
            if cursor.fetchone():
                return jsonify({"status": "duplicate", "message": "原文件已存在，跳过"}), 200

    record_id = str(uuid.uuid4())
    filepath = os.path.join(UPLOAD_FOLDER, f"{record_id}.jpg")
    file.save(filepath)
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO manuals (id, filename, filepath, original_hash, ocr_text, tags, status) VALUES (?, ?, ?, ?, ?, ?, ?)', 
                     (record_id, original_filename, filepath, original_hash, "", "[]", "uploaded"))
    
    return jsonify({"status": "success", "id": record_id})

@manuals_bp.route('/api/manuals/start', methods=['POST'])
def start_processing():
    ids = request.json.get('ids', [])
    with sqlite3.connect(DB_PATH) as conn:
        for record_id in ids:
            conn.execute("UPDATE manuals SET status = 'pending' WHERE id = ?", (record_id,))
            cursor = conn.cursor()
            cursor.execute("SELECT filepath FROM manuals WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if row: 
                executor.submit(process_manual_task, record_id, os.path.abspath(row[0]))
    return jsonify({"status": "started"})

@manuals_bp.route('/api/manuals/<record_id>', methods=['DELETE'])
def delete_manual(record_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM manuals WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            try: os.remove(row[0]) 
            except: pass
        conn.execute("DELETE FROM manuals WHERE id = ?", (record_id,))
    return jsonify({"status": "success"})

@manuals_bp.route('/api/manuals/retry', methods=['POST'])
def retry_manual():
    record_id = request.json.get('id')
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM manuals WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            conn.execute("UPDATE manuals SET status = 'pending' WHERE id = ?", (record_id,))
            executor.submit(process_manual_task, record_id, os.path.abspath(row[0]))
            return jsonify({"status": "queued"})
    return jsonify({"error": "Not found"}), 404

@manuals_bp.route('/api/manuals', methods=['GET'])
def get_manuals():
    query = "SELECT id, filename, filepath, tags, status, processing_time_sec, ocr_time_sec, nlp_time_sec, created_at, ocr_text FROM manuals ORDER BY created_at DESC"
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        
    return jsonify([{
        "id": r[0], 
        "filename": r[1] or '未命名文件',
        "url": f"/{r[2]}", 
        "tags": json.loads(r[3]) if r[3] else [],
        "status": r[4], 
        "metrics": {"total_sec": r[5], "ocr_sec": r[6], "nlp_sec": r[7]},
        "ocr_text": r[9]
    } for r in rows if r[4] != 'uploaded'])
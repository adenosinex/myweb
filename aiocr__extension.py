import os
import sqlite3
import json
import uuid
import time
import base64
import dashscope
from flask import Blueprint, request, jsonify
from concurrent.futures import ThreadPoolExecutor

manuals_bp = Blueprint('manuals', __name__)
DB_PATH = 'universal_data.db' 
UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

dashscope.api_key = os.getenv('DASHSCOPE_API_KEY')
executor = ThreadPoolExecutor(max_workers=3)

DEFAULT_OCR_MODEL = 'qwen-vl-max-latest' 
DEFAULT_NLP_MODEL = 'qwen-plus'

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

def process_manual_task(record_id, absolute_filepath, ocr_model_name, nlp_model_name):
    start_time = time.time()
    try:
        update_status(record_id, 'processing_ocr')
        
        with open(absolute_filepath, 'rb') as f:
            base64_img = base64.b64encode(f.read()).decode('utf-8')
        img_data_uri = f"data:image/jpeg;base64,{base64_img}"

        vl_messages = [{"role": "user", "content": [{"image": img_data_uri}, {"text": "请提取图中所有文字，严禁遗漏。使用Markdown格式呈现表格和标题，保持原有阅读顺序。"}]}]
        vl_response = dashscope.MultiModalConversation.call(model=ocr_model_name, messages=vl_messages)
        if vl_response.status_code != 200: raise Exception(f"OCR Error: {vl_response.message}")
        ocr_text = vl_response.output.choices[0].message.content[0]['text']
        
        ocr_duration = round(time.time() - start_time, 2)
        update_status(record_id, 'processing_nlp')
        
        prompt = f"""请提取2-5个凝练标签（如物品类、品牌、场景）。返回纯JSON数组，不要代码块。\n文本：{ocr_text[:2500]}"""
        text_response = dashscope.Generation.call(model=nlp_model_name, prompt=prompt, result_format='message')
        if text_response.status_code != 200: raise Exception(f"NLP Error: {text_response.message}")

        raw_tags = text_response.output.choices[0].message.content.strip()
        import re
        match = re.search(r'\[.*\]', raw_tags, re.DOTALL)
        tags_list = json.loads(match.group(0)) if match else ["未分类"]
        
        total_duration = round(time.time() - start_time, 2)

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                UPDATE manuals 
                SET ocr_text = ?, tags = ?, processing_time_sec = ?, ocr_time_sec = ?, nlp_time_sec = ?, status = 'completed'
                WHERE id = ?
            ''', (ocr_text, json.dumps(tags_list, ensure_ascii=False), total_duration, ocr_duration, total_duration - ocr_duration, record_id))
    except Exception as e:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE manuals SET status = 'failed', ocr_text = ? WHERE id = ?", (f"Error: {str(e)}", record_id))

# ================= 接口路由 =================
@manuals_bp.route('/api/manuals/upload', methods=['POST'])
def upload_manual():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    
    # 接收前端算好的【原图】Hash 和文件名
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
            if row: executor.submit(process_manual_task, record_id, os.path.abspath(row[0]), DEFAULT_OCR_MODEL, DEFAULT_NLP_MODEL)
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
            executor.submit(process_manual_task, record_id, os.path.abspath(row[0]), DEFAULT_OCR_MODEL, DEFAULT_NLP_MODEL)
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
        "filename": r[1] or '未命名文件', # 确保返回原始文件名
        "url": f"/{r[2]}", 
        "tags": json.loads(r[3]) if r[3] else [],
        "status": r[4], 
        "metrics": {"total_sec": r[5], "ocr_sec": r[6], "nlp_sec": r[7]},
        "ocr_text": r[9]
    } for r in rows if r[4] != 'uploaded'])
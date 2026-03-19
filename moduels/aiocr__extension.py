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
DB_PATH = 'db/universal_data.db' 
UPLOAD_FOLDER = 'db/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 增加 worker 数量以应对分离后的多阶段任务
executor = ThreadPoolExecutor(max_workers=5)

# ================= 模型接口配置 =================
OCR_CONFIG = {
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "api_key": os.getenv("SILICONFLOW_API_KEY"),
    "model": "Qwen/Qwen2.5-VL-32B-Instruct"
}

NLP_CONFIG = {
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "api_key": os.getenv("SILICONFLOW_API_KEY"),
    "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B"
}

# ================= 核心请求方法 =================
def call_openai_compatible_api(config, messages):
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

# ================= 解耦后的任务处理逻辑 =================

def process_ocr_task(record_id, absolute_filepath):
    """阶段 1：仅负责 OCR 提取并落盘"""
    start_time = time.time()
    try:
        update_status(record_id, 'processing_ocr')
        
        with open(absolute_filepath, 'rb') as f:
            base64_img = base64.b64encode(f.read()).decode('utf-8')
        img_data_uri = f"data:image/jpeg;base64,{base64_img}"

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
        ocr_text = ocr_text_output if isinstance(ocr_text_output, str) else str(ocr_text_output)
        ocr_duration = round(time.time() - start_time, 2)
        
        # 单独存储 OCR 结果，状态变更为等待 NLP
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                UPDATE manuals 
                SET ocr_text = ?, ocr_time_sec = ?, ocr_model = ?, status = 'pending_nlp'
                WHERE id = ?
            ''', (ocr_text, ocr_duration, OCR_CONFIG['model'], record_id))
            
        # OCR 成功后，自动触发 NLP 任务
        executor.submit(process_nlp_task, record_id)
            
    except Exception as e:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE manuals SET status = 'ocr_failed', ocr_text = ? WHERE id = ?", (f"OCR Error: {str(e)}", record_id))


def process_nlp_task(record_id):
    """阶段 2：仅负责 AI 标签分析（可单独重试）"""
    start_time = time.time()
    try:
        update_status(record_id, 'processing_nlp')
        
        # 从数据库读取已存的 OCR 文本
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ocr_text, ocr_time_sec FROM manuals WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if not row or not row[0] or row[0].startswith("OCR Error"):
                raise Exception("未找到有效的 OCR 提取文本")
            
            ocr_text = row[0]
            ocr_duration = float(row[1] or 0)
        
        nlp_messages = [
            {
                "role": "user", 
                "content": f"请提取2-5个凝练标签（如物品类、品牌、场景）。返回纯JSON数组，不要代码块。\n文本：{ocr_text[:2500]}"
            }
        ]
        
        raw_tags = call_openai_compatible_api(NLP_CONFIG, nlp_messages).strip()
        
        match = re.search(r'\[.*\]', raw_tags, re.DOTALL)
        tags_list = json.loads(match.group(0)) if match else ["未分类"]
        
        nlp_duration = round(time.time() - start_time, 2)
        total_duration = round(ocr_duration + nlp_duration, 2)

        # 存储最终结果
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                UPDATE manuals 
                SET tags = ?, nlp_time_sec = ?, processing_time_sec = ?, status = 'completed', nlp_model = ?
                WHERE id = ?
            ''', (json.dumps(tags_list, ensure_ascii=False), nlp_duration, total_duration, NLP_CONFIG['model'], record_id))
            
    except Exception as e:
        with sqlite3.connect(DB_PATH) as conn:
            # NLP 失败时，保留原有 ocr_text，只更新状态为 nlp_failed
            # 方便后续直接调用重新分析接口
            conn.execute("UPDATE manuals SET status = 'nlp_failed' WHERE id = ?", (record_id,))
            print(f"NLP Error for {record_id}: {str(e)}")

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
    """正常全量流程：启动 OCR，OCR 结束后自动触发 NLP"""
    ids = request.json.get('ids', [])
    with sqlite3.connect(DB_PATH) as conn:
        for record_id in ids:
            conn.execute("UPDATE manuals SET status = 'pending_ocr' WHERE id = ?", (record_id,))
            cursor = conn.cursor()
            cursor.execute("SELECT filepath FROM manuals WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if row: 
                executor.submit(process_ocr_task, record_id, os.path.abspath(row[0]))
    return jsonify({"status": "started"})
import os
from flask import send_file, abort
from pathlib import Path

@manuals_bp.route('/api/img/<imgname>', methods=['GET'])
def get_manual_image(imgname):
    """
    获取上传的图片文件
    """
    # 建议使用 Pathlib 处理路径，确保安全性
    img_path = Path(UPLOAD_FOLDER) / imgname
    
    # 检查文件是否存在且在目录内
    if img_path.is_file():
        return send_file(img_path)
    else:
        return abort(404, description="Image not found")

@manuals_bp.route('/api/manuals/retry', methods=['POST'])
def retry_manual():
    """全量重试：重新执行 OCR 和 NLP"""
    record_id = request.json.get('id')
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM manuals WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            conn.execute("UPDATE manuals SET status = 'pending_ocr', ocr_text = '', tags = '[]' WHERE id = ?", (record_id,))
            executor.submit(process_ocr_task, record_id, os.path.abspath(row[0]))
            return jsonify({"status": "queued_full_retry"})
    return jsonify({"error": "Not found"}), 404


@manuals_bp.route('/api/manuals/reanalyze', methods=['POST'])
def reanalyze_manual():
    """独立分析接口：跳过 OCR，仅使用已有文本重新进行 AI 分析"""
    record_id = request.json.get('id')
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, ocr_text FROM manuals WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        
        if not row:
            return jsonify({"error": "Not found"}), 404
        if not row[1] or row[1].startswith("OCR Error"):
            return jsonify({"error": "没有找到可用的OCR文本，请使用全量重试"}), 400
            
        conn.execute("UPDATE manuals SET status = 'pending_nlp', tags = '[]' WHERE id = ?", (record_id,))
        executor.submit(process_nlp_task, record_id)
        
    return jsonify({"status": "queued_nlp_only"})


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
       # 结果示例: /api/img/123.jpg
"url": f"/api/img/{r[0]}.jpg",
        "tags": json.loads(r[3]) if r[3] else [],
        "status": r[4], 
        "metrics": {"total_sec": r[5], "ocr_sec": r[6], "nlp_sec": r[7]},
        "ocr_text": r[9]
    } for r in rows if r[4] != 'uploaded'])
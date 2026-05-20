import os
import sqlite3
import json
import uuid
import time
import struct
import gzip
import math,requests
import traceback
from datetime import datetime
from flask import Blueprint, request, jsonify, send_file, abort
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import websocket
except ImportError:
    raise ImportError("缺少 websocket-client 库，请先执行: pip install websocket-client")

vocal_bp = Blueprint('vocal', __name__, url_prefix='/audiom')

DB_PATH = 'db/vocal_data.db'
UPLOAD_FOLDER = 'db/audio_uploads'
RAW_FOLDER = 'db/raw_responses'  
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RAW_FOLDER, exist_ok=True)

executor = ThreadPoolExecutor(max_workers=5)

# ================= 模型接口配置 =================
STT_CONFIG = {
    "api_key": os.getenv("DB_API_KEY", "your-volcengine-api-key"),
    # 注意：流式接口的 Resource ID 是 sauc 系列，请根据控制台实际开通的计费类型修改（duration 或 concurrent）
    "resource_id": "volc.seedasr.sauc.duration", 
    "ws_url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
}

NLP_CONFIG = {
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "api_key": os.getenv("SILICONFLOW_API_KEY"),
    "model": "deepseek-ai/DeepSeek-V4-Flash"
}

# ================= 数据库初始化 =================
def init_vocal_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS vocal_records (
                id TEXT PRIMARY KEY,
                filename TEXT,
                filepath TEXT,
                stt_text TEXT,
                sync_data TEXT,
                title_with_tags TEXT,
                duration_sec REAL,
                stt_model TEXT,
                nlp_model TEXT,
                stt_time_sec REAL,
                nlp_time_sec REAL,
                processing_time_sec REAL,
                status TEXT,
                created_at DATETIME DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        try: conn.execute("ALTER TABLE vocal_records ADD COLUMN duration_sec REAL")
        except: pass
init_vocal_db()

def update_status(record_id, status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE vocal_records SET status = ? WHERE id = ?", (status, record_id))

def get_fallback_title(record_id, suffix_tag=None):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT created_at, duration_sec FROM vocal_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            created_at, duration = row
            dur_str = f"[{int(duration)}s]" if duration else ""
            base_title = f"记录_{created_at.replace('-', '').replace(':', '').replace(' ', '_')} {dur_str}"
            return f"{base_title} #{suffix_tag}" if suffix_tag else base_title
    return f"未知记录 #{suffix_tag}" if suffix_tag else "未知记录"


# ================= WebSocket 协议打包工具 =================

def pack_full_client_request(req_json):
    """打包首包参数请求：类型1，序列化1(JSON)，压缩1(GZIP) -> 0x11101100"""
    payload = gzip.compress(json.dumps(req_json).encode('utf-8'))
    header = b'\x11\x10\x11\x00'
    size = struct.pack('>I', len(payload))
    return header + size + payload

def pack_audio_request(audio_chunk, is_last=False):
    """打包音频数据分包：类型2，压缩1(GZIP) -> 0x11200100。若是最后一包标志为2 -> 0x11220100"""
    payload = gzip.compress(audio_chunk)
    header = b'\x11\x22\x01\x00' if is_last else b'\x11\x20\x01\x00'
    size = struct.pack('>I', len(payload))
    return header + size + payload


# ================= 核心任务处理逻辑 =================

def process_stt_task(record_id, absolute_filepath):
    start_time = time.time()
    ws = None
    try:
        update_status(record_id, 'processing_stt')
        task_id = str(uuid.uuid4())
        
        # 1. 匹配音频格式 (Volcengine 严格要求 format)
        file_ext = os.path.splitext(absolute_filepath)[1].lower().strip('.')
        if file_ext in ['webm', 'ogg']:
            audio_format, codec = 'ogg', 'opus'
        elif file_ext in ['wav']:
            audio_format, codec = 'wav', 'raw'
        else:
            audio_format, codec = 'mp3', 'raw'
            
        # 2. 建立 WebSocket 连接
        headers = {
            "X-Api-Key": STT_CONFIG["api_key"],
            "X-Api-Resource-Id": STT_CONFIG["resource_id"],
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1"
        }
        
        ws = websocket.create_connection(STT_CONFIG["ws_url"], header=headers, timeout=15)
        
        # 3. 发送首包 (配置参数)
        req_json = {
            "user": {"uid": "vocalmind_user"},
            "audio": {
                "format": audio_format,
                "codec": codec,
                "rate": 16000,
                "bits": 16,
                "channel": 1,
                "language": "zh-CN"
            },
            "request": {
                "model_name": "bigmodel",
                "show_utterances": True, 
                "enable_punc": True,
                "enable_itn": True
            }
        }
        ws.send_binary(pack_full_client_request(req_json))
        
        # 4. 流式推送本地音频数据 (分包发送，控制节奏防止服务端过载)
        chunk_size = 8192
        with open(absolute_filepath, 'rb') as f:
            chunk = f.read(chunk_size)
            while chunk:
                next_chunk = f.read(chunk_size)
                is_last = not bool(next_chunk)
                ws.send_binary(pack_audio_request(chunk, is_last=is_last))
                chunk = next_chunk
                time.sleep(0.01) # 微小停顿，避免缓冲区拥堵
        
        # 5. 严格解析服务端二进制响应帧
        raw_response_data = None
        while True:
            resp = ws.recv()
            if not resp or len(resp) < 4:
                continue
                
            header_size = (resp[0] & 0x0F) * 4
            msg_type = (resp[1] & 0xF0) >> 4
            msg_flags = resp[1] & 0x0F
            msg_comp = resp[2] & 0x0F
            
            offset = header_size
            
            # Error message from server
            if msg_type == 0x0F:
                err_code = struct.unpack('>I', resp[offset:offset+4])[0]
                err_size = struct.unpack('>I', resp[offset+4:offset+8])[0]
                err_msg = resp[offset+8:offset+8+err_size].decode('utf-8')
                raise Exception(f"流式服务器错误 [{err_code}]: {err_msg}")
            
            # Full server response
            if msg_type == 0x09:
                # 检查 flags，如果是 1 或 3，说明携带了 4 字节的 Sequence
                if msg_flags in [0x01, 0x03]:
                    offset += 4
                
                payload_size = struct.unpack('>I', resp[offset:offset+4])[0]
                offset += 4
                payload_compressed = resp[offset:offset+payload_size]
                
                if msg_comp == 0x01:
                    payload_json = gzip.decompress(payload_compressed).decode('utf-8')
                else:
                    payload_json = payload_compressed.decode('utf-8')
                    
                parsed_res = json.loads(payload_json)
                
                if "result" in parsed_res:
                    raw_response_data = parsed_res
                    
                # flag == 2 或 3 表示这是服务端处理完成的最后一包结果
                if msg_flags in [0x02, 0x03]:
                    break

        if ws:
            ws.close()

        if not raw_response_data:
            raise Exception("未获取到有效的语音识别结果。")

        raw_filepath = os.path.join(RAW_FOLDER, f"{record_id}_raw.json")
        with open(raw_filepath, 'w', encoding='utf-8') as f:
            json.dump(raw_response_data, f, ensure_ascii=False, indent=2)

        result_dict = raw_response_data.get('result', {})
        full_text = result_dict.get('text', '')
        utterances = result_dict.get('utterances', [])
        
        sync_segments = [{"start": u.get("start_time", 0) / 1000.0, "end": u.get("end_time", 0) / 1000.0, "text": u.get("text", "")} for u in utterances]
        stt_duration = round(time.time() - start_time, 2)
        
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                UPDATE vocal_records 
                SET stt_text = ?, sync_data = ?, stt_time_sec = ?, stt_model = ?, status = 'pending_nlp'
                WHERE id = ?
            ''', (full_text, json.dumps(sync_segments, ensure_ascii=False), stt_duration, STT_CONFIG['resource_id'], record_id))
            
        executor.submit(process_nlp_task, record_id)
            
    except Exception as e:
        if ws:
            try: ws.close() 
            except: pass
            
        print(f"\n{'='*50}")
        print(f"[STT 任务异常] Record ID: {record_id}")
        traceback.print_exc()
        print(f"{'='*50}\n")
        
        fallback_title = get_fallback_title(record_id, "识别异常")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE vocal_records SET status = 'stt_failed', title_with_tags = ?, stt_text = ? WHERE id = ?", 
                         (fallback_title, f"STT Error: {str(e)}", record_id))

def process_nlp_task(record_id):
    start_time = time.time()
    try:
        update_status(record_id, 'processing_nlp')
        
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT stt_text, stt_time_sec FROM vocal_records WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if not row or not row[0] or "STT Error" in row[0]:
                raise Exception("无可用文本，已跳过 NLP 分析。")
            stt_text = row[0]
            stt_duration = float(row[1] or 0)
        
        messages = [
            {"role": "system", "content": "你是一个摘要助手。严格按格式输出：生成10字以内的核心标题，以及2-4个核心关键词。格式必须为：标题 #关键词1 #关键词2"},
            {"role": "user", "content": f"文本内容：\n{stt_text[:3000]}"}
        ]
        
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {NLP_CONFIG['api_key']}"}
        payload = {"model": NLP_CONFIG['model'], "messages": messages}
        response = requests.post(NLP_CONFIG['base_url'], headers=headers, json=payload, timeout=60)
        
        if response.status_code != 200:
            raise Exception(f"NLP API Error: {response.text}")
            
        title_with_tags = response.json()['choices'][0]['message']['content'].strip()
        nlp_duration = round(time.time() - start_time, 2)
        total_duration = round(stt_duration + nlp_duration, 2)

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                UPDATE vocal_records 
                SET title_with_tags = ?, nlp_time_sec = ?, processing_time_sec = ?, status = 'completed', nlp_model = ?
                WHERE id = ?
            ''', (title_with_tags, nlp_duration, total_duration, NLP_CONFIG['model'], record_id))
            
    except Exception as e:
        print(f"\n{'='*50}")
        print(f"[NLP 任务异常] Record ID: {record_id}")
        traceback.print_exc()
        print(f"{'='*50}\n")
        
        fallback_title = get_fallback_title(record_id, "摘要异常")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE vocal_records SET status = 'nlp_failed', title_with_tags = ? WHERE id = ?", (fallback_title, record_id))

# ================= 接口路由 =================

@vocal_bp.route('/api/vocal/upload', methods=['POST', 'OPTIONS'], strict_slashes=False)
def upload_audio():
    if request.method == 'OPTIONS':
        return '', 204

    if 'file' not in request.files: 
        return jsonify({"error": "No file"}), 400
    
    file = request.files['file']
    original_filename = request.form.get('original_filename', file.filename)
    
    raw_duration = request.form.get('duration', 0)
    try:
        duration_sec = float(raw_duration)
        if math.isinf(duration_sec) or math.isnan(duration_sec) or duration_sec < 0:
            duration_sec = 0.0
    except (ValueError, TypeError):
        duration_sec = 0.0
    
    record_id = str(uuid.uuid4())
    ext = os.path.splitext(original_filename)[1]
    if not ext: ext = '.webm'
    
    filepath = os.path.join(UPLOAD_FOLDER, f"{record_id}{ext}")
    file.save(filepath)
    
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    dur_str = f"[{int(duration_sec)}s]" if duration_sec > 0 else ""
    default_title = f"记录_{created_at.replace('-', '').replace(':', '').replace(' ', '_')} {dur_str} #未转录"

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO vocal_records (id, filename, filepath, duration_sec, stt_text, sync_data, title_with_tags, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', 
                     (record_id, original_filename, filepath, duration_sec, "", "[]", default_title, "uploaded", created_at))
    
    return jsonify({"status": "success", "id": record_id})

@vocal_bp.route('/api/vocal/start', methods=['POST', 'OPTIONS'], strict_slashes=False)
def start_processing():
    if request.method == 'OPTIONS':
        return '', 204

    ids = request.json.get('ids', [])
    with sqlite3.connect(DB_PATH) as conn:
        for record_id in ids:
            conn.execute("UPDATE vocal_records SET status = 'pending_stt' WHERE id = ?", (record_id,))
            cursor = conn.cursor()
            cursor.execute("SELECT filepath FROM vocal_records WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if row: 
                executor.submit(process_stt_task, record_id, os.path.abspath(row[0]))
    return jsonify({"status": "started"})

@vocal_bp.route('/api/vocal/retry', methods=['POST', 'OPTIONS'], strict_slashes=False)
def retry_processing():
    if request.method == 'OPTIONS':
        return '', 204

    record_id = request.json.get('id')
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath, status FROM vocal_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
            
        filepath, status = row
        
        if status == 'nlp_failed':
            conn.execute("UPDATE vocal_records SET status = 'pending_nlp' WHERE id = ?", (record_id,))
            executor.submit(process_nlp_task, record_id)
        else:
            conn.execute("UPDATE vocal_records SET status = 'pending_stt', stt_text = '' WHERE id = ?", (record_id,))
            executor.submit(process_stt_task, record_id, os.path.abspath(filepath))
            
    return jsonify({"status": "queued_retry"})

@vocal_bp.route('/api/vocal/audio/<record_id>', methods=['GET', 'OPTIONS'], strict_slashes=False)
def get_audio(record_id):
    if request.method == 'OPTIONS':
        return '', 204

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM vocal_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row and Path(row[0]).is_file():
            return send_file(Path(row[0]))
    return abort(404, description="Audio not found")

@vocal_bp.route('/api/vocal/<record_id>', methods=['DELETE', 'OPTIONS'], strict_slashes=False)
def delete_record(record_id):
    if request.method == 'OPTIONS':
        return '', 204

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM vocal_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            try: os.remove(row[0]) 
            except: pass
            
            raw_path = os.path.join(RAW_FOLDER, f"{record_id}_raw.json")
            if os.path.exists(raw_path):
                try: os.remove(raw_path)
                except: pass
                
        conn.execute("DELETE FROM vocal_records WHERE id = ?", (record_id,))
    return jsonify({"status": "success"})

@vocal_bp.route('/api/vocal', methods=['GET', 'OPTIONS'], strict_slashes=False)
def get_records():
    if request.method == 'OPTIONS':
        return '', 204

    query = "SELECT id, filename, filepath, title_with_tags, status, stt_text, sync_data, created_at FROM vocal_records ORDER BY created_at DESC"
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        
    return jsonify([{
        "id": r[0], 
        "filename": r[1] or '未命名录音',
        "audio_url": f"/audiom/api/vocal/audio/{r[0]}",
        "title_with_tags": r[3] or '',
        "status": r[4],
        "stt_text_preview": (r[5][:60] + '...') if r[5] else '',
        "sync_data": json.loads(r[6]) if r[6] else [],
        "created_at": r[7]
    } for r in rows if r[4] != 'uploaded'])
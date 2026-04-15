import os
import sqlite3
import json,re
import uuid
import time
import base64
import requests
from flask import Blueprint, request, jsonify, send_file, abort,send_from_directory
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

snapshots_bp = Blueprint('snapshots', __name__)
DB_PATH = 'db/universal_datasmall.db' 
UPLOAD_FOLDER = 'db/history'
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
    record_time_raw = request.form.get('record_time')
    
    # 1. 规范化时间处理
    if record_time_raw:
        try:
            # 解析前端传来的 YYYY-MM-DD HH:mm 格式
            dt_obj = datetime.strptime(record_time_raw.strip(), '%Y-%m-%d %H:%M')
            record_time_db = record_time_raw  # 保持原有格式入库
        except ValueError:
            dt_obj = datetime.now()
            record_time_db = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
    else:
        dt_obj = datetime.now()
        record_time_db = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
        
    # 2. 生成带时间戳的文件名 ID (格式: YYYYMMDD_HHMM_4位随机码)
    # 附加短随机码是为了防止同一分钟内批量上传导致重名覆盖
    time_prefix = dt_obj.strftime('%Y%m%d_%H%M')
    record_id = f"{time_prefix}_{uuid.uuid4().hex[:4]}"
    
    filepath = os.path.join(UPLOAD_FOLDER, f"{record_id}.jpg")
    file.save(filepath)
    
    # 3. 统一入库逻辑，消除冗余分支
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO snapshots (id, event_type, filepath, bg_description, tags, status, record_time) VALUES (?, ?, ?, ?, ?, ?, ?)', 
            (record_id, event_type, filepath, "", "[]", "completed", record_time_db)
        )
    
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

# ================= 通用数据追踪与资产看板路由 (SQLite 版) =================

def init_tracker_db():
    """初始化通用追踪看板数据库表"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS universal_tracking (
                id TEXT,
                board_type TEXT,  -- 新增字段：用于区分不同的看板网页数据
                payload TEXT,     -- 存储该项的详细 JSON 数据
                updated_at DATETIME DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (id, board_type)
            )
        ''')
init_tracker_db()
 

# 配置上传目录
UPLOAD_FOLDER = 'db/assert-cloud'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def save_base64_image(b64_str, filename):
    """解析 base64 并保存为文件，返回相对路径"""
    if not b64_str or not str(b64_str).startswith('data:image'):
        return b64_str
    
    try:
        # 提取 base64 数据部分
        header, data = b64_str.split(',', 1)
        # 获取后缀 (png, jpg, etc.)
        ext = re.search(r'/(.*?);', header).group(1)
        full_filename = f"{filename}.{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, full_filename)
        
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(data))
        
        # 返回浏览器可访问的 URL 路径
        return f"/{filepath}" 
    except Exception as e:
        print(f"图片保存失败: {e}")
        return b64_str

def process_items_images(items, board_type):
    """遍历 items，处理 image 和 receiptImage 字段"""
    for item in items:
        item_id = item.get('id')
        # 处理主图
        if 'image' in item and str(item['image']).startswith('data:image'):
            item['image'] = save_base64_image(item['image'], f"{board_type}_{item_id}_main")
        # 处理发票/凭证图
        if 'receiptImage' in item and str(item['receiptImage']).startswith('data:image'):
            item['receiptImage'] = save_base64_image(item['receiptImage'], f"{board_type}_{item_id}_receipt")
    return items

def universal_sync_logic(board_type):
    """通用同步逻辑：处理图片并保存 JSON 到数据库"""
    if request.method == 'POST':
        data = request.json
        if not data or 'items' not in data:
            return jsonify({"error": "Empty data"}), 400
            
        # 1. 关键：处理 Base64 图片落地
        processed_items = process_items_images(data['items'], board_type)
        
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM universal_tracking WHERE board_type = ?", (board_type,))
            for item in processed_items:
                item_id = item.get('id')
                if item_id:
                    cursor.execute(
                        "INSERT INTO universal_tracking (id, board_type, payload) VALUES (?, ?, ?)",
                        (item_id, board_type, json.dumps(item, ensure_ascii=False))
                    )
        return jsonify({"status": "success"})
    else:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload FROM universal_tracking WHERE board_type = ?", (board_type,))
            rows = cursor.fetchall()
        items = []
        for row in rows:
            item = json.loads(row[0])
            for field in ['image', 'receiptImage']:
                val = item.get(field)
                
                if val and isinstance(val, str):
                    # 1. 提取文件名：无论 val 是 "/a/b/c.png" 还是 "c.png"，都只取 "c.png"
                    file_name = val.split('/')[-1]
                    
                    # 2. 排除掉没图的情况（比如 val 只是 "/" 或空字符）
                    if file_name and '.' in file_name:
                        # 3. 统一拼接成前端路由格式
                        item[field] = f"/tracker/image/{file_name}"
                    else:
                        item[field] = "" # 无效路径置空
                        
            items.append(item)
        
        return jsonify([{"items": items}])

# ----------------- 路由执行 -----------------

@snapshots_bp.route('/tracker_data/sync/daily', methods=['GET', 'POST'])
def sync_daily_data():
    return universal_sync_logic('daily_data')

@snapshots_bp.route('/tracker_data/sync/assets', methods=['GET', 'POST'])
def sync_assets_data():
    return universal_sync_logic('assets_data')

@snapshots_bp.route('/tracker/image/<filename>')
def get_tracker_image(filename):
    """根据文件名 ID 获取图片"""
    # 使用 send_from_directory 可以自动处理文件是否存在、MIME类型及安全路径校验
    return send_from_directory(UPLOAD_FOLDER, filename)

def run_migration():
    """执行迁移：Base64 -> 文件存储"""
  
    
    print("--- 启动数据迁移任务 ---")
    
    if not os.path.exists(DB_PATH):
        print(f"错误: 找不到数据库文件 {DB_PATH}")
        return

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            # 1. 备份数据（可选，建议操作）
            cursor.execute("CREATE TABLE IF NOT EXISTS universal_tracking_backup AS SELECT * FROM universal_tracking")
            print("已创建备份表: universal_tracking_backup")

            # 2. 读取所有记录
            cursor.execute("SELECT id, board_type, payload FROM universal_tracking")
            rows = cursor.fetchall()
            print(f"找到 {len(rows)} 条待扫描记录")

            updated_count = 0
             
            for item_id, board_type, payload_str in rows:
                try:
                    item = json.loads(payload_str)
                except:
                    continue
                
                changed = False
                
                # 处理主图
                if 'image' in item:
                    new_val = save_base64_image(item['image'], f"{board_type}_{item_id}_main")
                    if new_val != item['image']:
                        item['image'] = new_val
                        changed = True
                
                # 处理凭证图
                if 'receiptImage' in item:
                    new_val = save_base64_image(item['receiptImage'], f"{board_type}_{item_id}_receipt")
                    if new_val != item['receiptImage']:
                        item['receiptImage'] = new_val
                        changed = True
                
                # 3. 如果数据有变动，回写数据库
                if changed:
                    new_payload = json.dumps(item, ensure_ascii=False)
                    cursor.execute(
                        "UPDATE universal_tracking SET payload = ? WHERE id = ? AND board_type = ?",
                        (new_payload, item_id, board_type)
                    )
                    updated_count += 1

            conn.commit()
            print(f"--- 迁移完成 ---")
            print(f"成功迁移记录数: {updated_count}")

    except Exception as e:
        print(f"数据库操作异常: {e}")
# run_migration()
# print('----------done')

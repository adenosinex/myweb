import os
import sqlite3
import json
import csv
import io
import requests
from flask import Blueprint, request, jsonify, Response, send_file

app_bp = Blueprint('app', __name__, url_prefix='/app')

# 强制使用绝对路径，防止主程序启动目录变动导致数据库丢失
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'db/app_data.db')
RESOURCE_NODE_URL = 'http://15x4.su7.dpdns.org:5001'  # 设定资源节点地址

AI_API_URL = os.getenv("MODEL_SI_URL")  
AI_API_KEY = os.getenv("SI_API_KEY")               
AI_MODEL = "deepseek-ai/DeepSeek-V4-Flash"                                  

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS software (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                filename TEXT UNIQUE NOT NULL,
                size TEXT DEFAULT '0 B',
                tags TEXT DEFAULT '[]',
                description TEXT DEFAULT ''
            )
        ''')
        cursor.execute("PRAGMA table_info(software)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'size' not in columns:
            cursor.execute("ALTER TABLE software ADD COLUMN size TEXT DEFAULT '0 B'")
        conn.commit()

init_db()

@app_bp.route('/upload', methods=['POST'])
def upload_proxy():
    if 'file' not in request.files: 
        return jsonify({"status": "error", "message": "No file"}), 400
    file = request.files['file']
    original_name = file.filename
    files = {'file': (original_name, file.stream, file.mimetype)}
    try:
        res = requests.post(f"{RESOURCE_NODE_URL}/upload", files=files)
        node_data = res.json()
    except Exception as e:
        return jsonify({"status": "error", "message": f"节点连接失败: {str(e)}"}), 500

    if node_data.get('status') != 'success': 
        return jsonify({"status": "error", "message": "节点处理失败"}), 500

    sha256 = node_data['sha256']
    filename = node_data['filename']
    file_size = node_data.get('size', '0 B')
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO software (name, sha256, filename, size, tags, description)
            VALUES (?, ?, ?, ?, '[]', '')
        ''', (original_name, sha256, filename, file_size))
        conn.commit()
    return jsonify({"status": "success", "sha256": sha256, "name": original_name})

@app_bp.route('/download/<int:sw_id>', methods=['GET'])
def download_proxy(sw_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT name, filename FROM software WHERE id = ?', (sw_id,))
        row = cursor.fetchone()
    
    if not row: return "Record not found", 404
        
    display_name, real_filename = row[0], row[1]
    # 当前端直接连接失败时，此接口作为兜底转发流
    req = requests.get(f"{RESOURCE_NODE_URL}/download", params={"filename": real_filename}, stream=True)
    return Response(
        req.iter_content(chunk_size=1024*1024), 
        content_type=req.headers.get('Content-Type', 'application/octet-stream'),
        headers={"Content-Disposition": f"attachment; filename={display_name.encode('utf-8').decode('latin-1')}"}
    )

@app_bp.route('/scan', methods=['GET'])
def scan_files():
    try:
        res = requests.get(f"{RESOURCE_NODE_URL}/list")
        node_files = res.json().get('files', [])
    except Exception as e:
        return jsonify({"status": "error", "message": f"节点通信失败: {str(e)}"}), 500

    # 1. 构建物理节点的数据映射 (基于 SHA256)
    physical_map = {f['sha256']: f for f in node_files}
    physical_shas = set(physical_map.keys())

    # 2. 获取数据库的数据映射
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT id, sha256, filename FROM software')
        db_rows = cursor.fetchall()
        
    db_shas = set([row['sha256'] for row in db_rows])
    db_map = {row['sha256']: row['filename'] for row in db_rows}

    auto_indexed_count = 0
    path_updated_count = 0

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # A. 物理位置发生移动时（静默更新路径指针，对前端与数据库ID完全透明）
        for sha in physical_shas.intersection(db_shas):
            if physical_map[sha]['filename'] != db_map[sha]:
                cursor.execute('UPDATE software SET filename = ? WHERE sha256 = ?', (physical_map[sha]['filename'], sha))
                path_updated_count += 1

        # B. 录入新被拖入节点磁盘的资源
        unindexed_shas = physical_shas - db_shas
        for sha in unindexed_shas:
            f = physical_map[sha]
            show_name = os.path.basename(f['filename'])
            cursor.execute('''
                INSERT OR IGNORE INTO software (name, sha256, filename, size, tags, description)
                VALUES (?, ?, ?, ?, '[]', '')
            ''', (show_name, sha, f['filename'], f['size']))
            auto_indexed_count += 1

        # C. 清理已被物理删除的游离记录
        missing_shas = db_shas - physical_shas
        deleted_count = len(missing_shas)
        if missing_shas:
            cursor.executemany('DELETE FROM software WHERE sha256 = ?', [(sha,) for sha in missing_shas])
            
        conn.commit()

    # 3. 根目录容量检测与动态归类机制 (大于15个文件时触发)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # 判断标准：没有斜杠说明在根目录
        cursor.execute("SELECT sha256, filename, tags FROM software WHERE filename NOT LIKE '%/%' AND filename NOT LIKE '%\\%'")
        root_files = cursor.fetchall()

        if len(root_files) > 15:
            moves = []
            for f in root_files:
                try: tags = json.loads(f['tags']) if f['tags'] else []
                except: tags = []
                
                tag_folder = tags[0] if tags else "未分类"
                # 清洗文件名，防止非法路径字符
                tag_folder = "".join(c for c in tag_folder if c.isalnum() or c in (" ", "-", "_", "\u4e00-\u9fa5")).strip()
                if not tag_folder: tag_folder = "未分类"

                old_path = f['filename']
                new_path = f"{tag_folder}/{old_path}"
                moves.append({"old_path": old_path, "new_path": new_path, "sha256": f['sha256']})

            if moves:
                try:
                    # 指挥资源节点自己移动文件
                    organize_res = requests.post(f"{RESOURCE_NODE_URL}/organize", json={"moves": moves})
                    if organize_res.status_code == 200:
                        # 移动成功后同步数据库指针
                        for m in moves:
                            cursor.execute("UPDATE software SET filename = ? WHERE sha256 = ?", (m['new_path'], m['sha256']))
                        conn.commit()
                        path_updated_count += len(moves)
                except Exception as e:
                    print("[节点指令下发失败] 无法连通 /organize 接口: ", e)

    return jsonify({
        "status": "success",
        "message": f"同步完成。入库 {auto_indexed_count}，清理 {deleted_count}，位置自适应调整 {path_updated_count} 个。",
        "total_physical": len(physical_shas),
        "total_db": len(db_shas) + auto_indexed_count - deleted_count
    })

@app_bp.route('/ai_enhance/<int:sw_id>', methods=['POST'])
def ai_enhance(sw_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT name FROM software WHERE id = ?', (sw_id,))
        row = cursor.fetchone()
    if not row:
        return jsonify({"status": "error", "message": "未找到该软件记录"}), 404
        
    software_name = row[0]
    
    prompt = f"""
    你是一个软件数据分析助手。请分析以下软件文件名，推断其对应的真实软件。
    输入文件名: {software_name}
    
    请输出 JSON 格式的结果，包含以下两个字段：
    1. "tags": 数组格式，提取3-5个描述该软件核心功能或技术栈的中文短标签。
    2. "description": 字符串格式，合成一段结构清晰的中文综合简介。简介中必须包含：该软件的官方中文译名、软件的来龙去脉（背景与开发源头）、以及它在家庭局域网或日常中的核心用途。
    
    请严格返回合法的 JSON 对象，不要包含任何 Markdown 格式包裹（如 ```json ），不要有多余的解释文字。
    """
    
    try:
        headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }
        res = requests.post(AI_API_URL, json=payload, headers=headers, timeout=15)
        ai_res = res.json()
        content = ai_res['choices'][0]['message']['content'].strip()
        
        result_data = json.loads(content)
        tags_json = json.dumps(result_data.get('tags', []), ensure_ascii=False)
        description = result_data.get('description', '')
        
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE software SET tags = ?, description = ? WHERE id = ?
            ''', (tags_json, description, sw_id))
            conn.commit()
            
        return jsonify({"status": "success", "tags": result_data.get('tags', []), "description": description})
    except Exception as e:
        return jsonify({"status": "error", "message": f"AI 分析失败: {str(e)}"}), 500

@app_bp.route('/export_csv', methods=['GET'])
def export_csv():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id, name, sha256, filename, size, tags, description FROM software')
        rows = cursor.fetchall()

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['id', 'name', 'sha256', 'filename', 'size', 'tags_json', 'description'])
    for r in rows: cw.writerow(r)
        
    output = io.BytesIO(si.getvalue().encode('utf-8-sig'))
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name='software_index.csv')

@app_bp.route('/import_csv', methods=['POST'])
def import_csv():
    if 'file' not in request.files: 
        return jsonify({"status": "error", "message": "No file"}), 400
    file = request.files['file']
    stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
    csv_input = csv.DictReader(stream)
    
    updated_count = 0
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for row in csv_input:
            try:
                tags_raw = row['tags_json'].strip() if row.get('tags_json') else '[]'
                if not tags_raw: tags_raw = '[]'
                json.loads(tags_raw)
                
                cursor.execute('''
                    UPDATE software SET name = ?, tags = ?, description = ? WHERE id = ?
                ''', (row['name'], tags_raw, row.get('description', ''), row['id']))
                updated_count += 1
            except: 
                continue
        conn.commit()
    return jsonify({"status": "success", "updated_rows": updated_count})

@app_bp.route('/search', methods=['GET'])
def search():
    query = request.args.get('q', '')
    page = int(request.args.get('page', 1))
    per_page = 20
    offset = (page - 1) * per_page
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        if query:
            cursor.execute('''
                SELECT COUNT(*) FROM software 
                WHERE name LIKE ? OR tags LIKE ? OR filename LIKE ? OR description LIKE ?
            ''', (f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%'))
            total_items = cursor.fetchone()[0]
            
            cursor.execute('''
                SELECT * FROM software 
                WHERE name LIKE ? OR tags LIKE ? OR filename LIKE ? OR description LIKE ?
                LIMIT ? OFFSET ?
            ''', (f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%', per_page, offset))
        else:
            cursor.execute('SELECT COUNT(*) FROM software')
            total_items = cursor.fetchone()[0]
            
            cursor.execute('SELECT * FROM software LIMIT ? OFFSET ?', (per_page, offset))
            
        rows = cursor.fetchall()
        
    results = []
    for r in rows:
        try: tags = json.loads(r["tags"]) if r["tags"] else []
        except: tags = []
            
        results.append({
            "id": r["id"], 
            "name": r["name"], 
            "sha256": r["sha256"], 
            "filename": r["filename"],
            "size": r["size"], 
            "tags": tags, 
            "description": r["description"] or ''
        })
        
    return jsonify({
        "results": results,
        "total_items": total_items,
        "page": page,
        "per_page": per_page,
        "total_pages": (total_items + per_page - 1) // per_page,
        "resource_node_url": RESOURCE_NODE_URL
    })
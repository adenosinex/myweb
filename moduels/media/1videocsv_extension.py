import os
import sqlite3
import time
import csv
from io import StringIO, BytesIO
from flask import Flask, Blueprint, request, jsonify, abort, send_file, render_template

# ================= 基础配置与目录初始化 =================
BASE_DIR = os.path.abspath("db/videocsv")
PAGES_DIR = os.path.join(BASE_DIR, "csvsvideo")
DB_FILE = os.path.join(BASE_DIR, "videos_csv.db")

os.makedirs(PAGES_DIR, exist_ok=True)

csv_bp = Blueprint('csv_api', __name__, url_prefix='/csv')

# ================= 数据库交互基类 =================
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # 核心优化：开启WAL模式与加大缓存池，极大提升读取速度与并发性能
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=-64000') # 64MB Cache
    return conn
    
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # 文件批次表
    c.execute('''
        CREATE TABLE IF NOT EXISTS csv_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            upload_time REAL
        )
    ''')
    # 视频记录表 - 核心修改：path 字段增加 UNIQUE 约束
    c.execute('''
        CREATE TABLE IF NOT EXISTS video_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            csv_id INTEGER,
            path TEXT UNIQUE,
            size TEXT,
            mtime TEXT,
            head_hash TEXT,
            sample_hash TEXT,
            width TEXT,
            height TEXT,
            codec TEXT,
            bitrate TEXT,
            duration TEXT,
            fps TEXT
        )
    ''')
    # 建立索引：加速局部查询。path 因设置为 UNIQUE 会自动创建索引，故移除了原有的 path 索引
    c.execute('CREATE INDEX IF NOT EXISTS idx_video_csv_id ON video_records(csv_id)')
    conn.commit()
    conn.close()

# 模块加载时立即执行初始化
init_db()

# ================= API 路由逻辑 =================

 
@csv_bp.route('/files', methods=['GET'])
def list_files():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM csv_files ORDER BY upload_time DESC")
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@csv_bp.route('/search', methods=['GET'])
def search():
    q = request.args.get('q', '').strip()
    csv_id = request.args.get('csv_id', '')
    limit = int(request.args.get('limit', 1000)) # 限制前端渲染条数避免卡死
    
    conn = get_db_connection()
    c = conn.cursor()
    
    query = "SELECT * FROM video_records WHERE 1=1"
    params = []
    
    if csv_id:
        query += " AND csv_id = ?"
        params.append(csv_id)
        
    if q:
        query += " AND path LIKE ?"
        params.append(f'%{q}%')
        
    query += " LIMIT ?"
    params.append(limit)
    
    start_time = time.time()
    c.execute(query, params)
    rows = c.fetchall()
    cost_time = time.time() - start_time
    
    results = [dict(row) for row in rows]
    conn.close()
    
    return jsonify({"results": results, "cost_time": round(cost_time, 4), "count": len(results)})

@csv_bp.route('/upload', methods=['POST'])
def upload_csv():
    if 'file' not in request.files:
        return jsonify({"error": "缺少文件"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "未选择文件"}), 400

    filename = file.filename
    raw_bytes = file.read()  # 读取原始字节，用于物理保存
    
    try:
        content = raw_bytes.decode('utf-8-sig')
    except Exception as e:
        return jsonify({"error": f"文件编码解析失败，请确保为 UTF-8 编码: {str(e)}"}), 400
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO csv_files (filename, upload_time) VALUES (?, ?)", (filename, time.time()))
    csv_id = c.lastrowid
    
    # 将原始文件物理保存到硬盘，格式为：ID_原文件名，避免重名冲突
    save_path = os.path.join(PAGES_DIR, f"{csv_id}_{filename}")
    with open(save_path, 'wb') as f:
        f.write(raw_bytes)
    
    # 分隔符识别逻辑
    first_line = content.split('\n')[0] if content else ""
    if '\t' in first_line and ',' not in first_line:
        delimiter = '\t'
    elif ',' in first_line:
        delimiter = ','
    else:
        try:
            dialect = csv.Sniffer().sniff(content[:4096])
            delimiter = dialect.delimiter
        except:
            delimiter = ','
            
    reader = csv.DictReader(StringIO(content), delimiter=delimiter)

    records = []
    for row in reader:
        if not any(row.values()):
            continue
        records.append((
            csv_id,
            row.get('path', '').strip() if row.get('path') else '', 
            row.get('size', '').strip() if row.get('size') else '', 
            row.get('mtime', '').strip() if row.get('mtime') else '',
            row.get('head_hash', '').strip() if row.get('head_hash') else '', 
            row.get('sample_hash', '').strip() if row.get('sample_hash') else '', 
            row.get('width', '').strip() if row.get('width') else '',
            row.get('height', '').strip() if row.get('height') else '', 
            row.get('codec', '').strip() if row.get('codec') else '', 
            row.get('bitrate', '').strip() if row.get('bitrate') else '',
            row.get('duration', '').strip() if row.get('duration') else '', 
            row.get('fps', '').strip() if row.get('fps') else ''
        ))
    
    if not records:
        conn.close()
        # 如果解析失败，清理刚才保存的无效文件
        if os.path.exists(save_path):
            os.remove(save_path)
        return jsonify({"error": "未从文件中解析出任何有效行，请检查字段列名是否与标准格式一致"}), 400

    # 基于 path 的 UPSERT 逻辑
    c.executemany('''
        INSERT INTO video_records (
            csv_id, path, size, mtime, head_hash, sample_hash, 
            width, height, codec, bitrate, duration, fps
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            csv_id = excluded.csv_id,
            size = excluded.size,
            mtime = excluded.mtime,
            head_hash = excluded.head_hash,
            sample_hash = excluded.sample_hash,
            width = excluded.width,
            height = excluded.height,
            codec = excluded.codec,
            bitrate = excluded.bitrate,
            duration = excluded.duration,
            fps = excluded.fps
        WHERE video_records.size IS NOT excluded.size
           OR video_records.mtime IS NOT excluded.mtime
           OR video_records.head_hash IS NOT excluded.head_hash
           OR video_records.sample_hash IS NOT excluded.sample_hash
           OR video_records.width IS NOT excluded.width
           OR video_records.height IS NOT excluded.height
           OR video_records.codec IS NOT excluded.codec
           OR video_records.bitrate IS NOT excluded.bitrate
           OR video_records.duration IS NOT excluded.duration
           OR video_records.fps IS NOT excluded.fps
    ''', records)
    
    affected_rows = c.rowcount
    conn.commit()
    conn.close()
    
    return jsonify({
        "message": "上传并解析成功", 
        "csv_id": csv_id, 
        "total_parsed": len(records),
        "inserted_or_updated": affected_rows
    })

@csv_bp.route('/download', methods=['GET'])
def download():
    csv_id = request.args.get('csv_id', '')
    conn = get_db_connection()
    c = conn.cursor()
    
    if csv_id:
        # 当指定文件 ID 时，优先查找并返回保存在本地的原文件
        c.execute("SELECT filename FROM csv_files WHERE id = ?", (csv_id,))
        row = c.fetchone()
        if row:
            original_filename = row['filename']
            file_path = os.path.join(PAGES_DIR, f"{csv_id}_{original_filename}")
            if os.path.exists(file_path):
                conn.close()
                return send_file(file_path, as_attachment=True, download_name=original_filename)
        
        # 若原文件丢失（如被清理），降级为从数据库查询重构数据导出
        c.execute("SELECT * FROM video_records WHERE csv_id = ?", (csv_id,))
        export_name = f"export_{csv_id}.csv"
    else:
        # 导出全局所有数据
        c.execute("SELECT * FROM video_records")
        export_name = "export_all.csv"
        
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return abort(404, "无数据可下载")
        
    si = StringIO()
    fieldnames = ['path', 'size', 'mtime', 'head_hash', 'sample_hash', 'width', 'height', 'codec', 'bitrate', 'duration', 'fps']
    writer = csv.DictWriter(si, fieldnames=fieldnames, delimiter='\t')
    writer.writeheader()
    for row in rows:
        filtered_row = {k: row[k] for k in fieldnames}
        writer.writerow(filtered_row)
        
    output = BytesIO()
    output.write(si.getvalue().encode('utf-8-sig'))
    output.seek(0)
    
    return send_file(output, as_attachment=True, download_name=export_name, mimetype='text/csv')
 
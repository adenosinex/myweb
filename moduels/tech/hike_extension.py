import os
import sqlite3
import csv
import math
import base64
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, send_file
import pandas as pd

# ================= 基础配置与目录初始化 =================
BASE_DIR = os.path.abspath("db/hike_predict")
PROJECTS_BASE_DIR = os.path.join(BASE_DIR, "projects")
DB_FILE = os.path.join(BASE_DIR, "hike_projects.db")

# 初始化基础目录
for path in [BASE_DIR, PROJECTS_BASE_DIR]:
    os.makedirs(path, exist_ok=True)

hike_bp = Blueprint('hike', __name__, url_prefix='/hike', template_folder='templates')

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gpx_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                original_name TEXT NOT NULL,
                gpx_path TEXT NOT NULL,
                csv_path TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        """)
init_db()

# ================= 工具函数 =================
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0 
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def parse_gpx_to_csv(gpx_path, csv_path):
    try:
        it = ET.iterparse(gpx_path)
        for _, el in it:
            el.tag = el.tag.split('}', 1)[1] if '}' in el.tag else el.tag
        root = it.root

        points = []
        for trkpt in root.iter('trkpt'):
            lat = float(trkpt.get('lat'))
            lon = float(trkpt.get('lon'))
            
            # 提取时间
            time_el = trkpt.find('time')
            if time_el is not None and time_el.text:
                time_str = time_el.text.replace('Z', '+00:00')
                dt = datetime.fromisoformat(time_str)
            else:
                continue # 缺少时间戳的点对于测算无意义，直接丢弃
                
            # 提取海拔（部分弱 GPS 信号点可能无海拔，默认给 0）
            ele_el = trkpt.find('ele')
            ele = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
            
            points.append((lat, lon, dt, ele))

        if not points: return False

        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            # 扩充表头维度
            writer.writerow(['lat', 'lon', 'distance_km', 'time_hours', 'elevation_m', 'timestamp'])
            
            total_dist = 0.0
            start_time = points[0][2]
            
            # 写入起点
            writer.writerow([
                points[0][0], points[0][1], 
                0.0, 0.0, 
                points[0][3], int(start_time.timestamp())
            ])

            for i in range(1, len(points)):
                lat1, lon1, t1, _ = points[i-1]
                lat2, lon2, t2, ele2 = points[i]
                
                dist = haversine(lat1, lon1, lat2, lon2)
                total_dist += dist
                time_diff = (t2 - start_time).total_seconds() / 3600.0
                
                writer.writerow([
                    lat2, lon2, 
                    round(total_dist, 4), round(time_diff, 4), 
                    round(ele2, 1), int(t2.timestamp())
                ])
        return True
    except Exception as e:
        print(f"GPX parse error: {e}")
        return False

def predict_remaining_time(gpx_records: list[dict], current_distance: float, current_time: float):
    best_match = None
    min_time_diff = float('inf')
    ref_current_time = 0.0

    for record in gpx_records:
        df = record['df']
        if df.empty or df['distance_km'].max() < current_distance:
            continue

        idx = (df['distance_km'] - current_distance).abs().idxmin()
        closest_distance = df.loc[idx, 'distance_km']
        
        if abs(closest_distance - current_distance) > 0.5:
            continue
            
        gpx_time_at_dist = df.loc[idx, 'time_hours']
        if gpx_time_at_dist <= 0.01:
            continue

        time_diff = abs(current_time - gpx_time_at_dist)
        if time_diff < min_time_diff:
            min_time_diff = time_diff
            best_match = record
            ref_current_time = gpx_time_at_dist

    if best_match is None:
        return 0.0, None

    ref_total_time = best_match['df']['time_hours'].iloc[-1]
    ref_remaining_time = ref_total_time - ref_current_time
    pace_ratio = current_time / ref_current_time
    
    return ref_remaining_time * pace_ratio, best_match['name']

# ================= 路由接口 =================

@hike_bp.route('/')
def index():
    return render_template('hike_index.html')

@hike_bp.route('/api/projects', methods=['GET', 'POST'])
def handle_projects():
    if request.method == 'POST':
        name = request.json.get('name')
        if not name: return jsonify({'error': '名称不能为空'}), 400
        try:
            with get_db() as conn:
                cursor = conn.execute("INSERT INTO projects (name) VALUES (?)", (name,))
                return jsonify({'id': cursor.lastrowid, 'name': name})
        except sqlite3.IntegrityError:
            return jsonify({'error': '项目已存在'}), 400
            
    with get_db() as conn:
        projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return jsonify([dict(p) for p in projects])

@hike_bp.route('/api/projects/<int:project_id>/records', methods=['GET'])
def get_project_records(project_id):
    with get_db() as conn:
        # 把 gpx_path 也查出来，用于计算文件大小
        records = conn.execute("SELECT id, original_name, gpx_path FROM gpx_records WHERE project_id = ? ORDER BY id DESC", (project_id,)).fetchall()
    
    result = []
    for r in records:
        r_dict = dict(r)
        size_str = "未知大小"
        
        # 动态计算物理文件大小
        if os.path.exists(r_dict['gpx_path']):
            size_bytes = os.path.getsize(r_dict['gpx_path'])
            if size_bytes < 1024 * 1024:
                size_str = f"{size_bytes / 1024:.1f} KB"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.2f} MB"
                
        r_dict['size_formatted'] = size_str
        del r_dict['gpx_path'] # 保护服务器隐私，不向前端暴露绝对路径
        result.append(r_dict)
        
    return jsonify(result)

@hike_bp.route('/api/download/<int:record_id>', methods=['GET'])
def download_gpx(record_id):
    with get_db() as conn:
        record = conn.execute("SELECT gpx_path, original_name FROM gpx_records WHERE id = ?", (record_id,)).fetchone()
    
    if record and os.path.exists(record['gpx_path']):
        return send_file(record['gpx_path'], as_attachment=True, download_name=record['original_name'])
    return jsonify({'error': '文件不存在'}), 404

@hike_bp.route('/api/upload', methods=['POST'])
def upload_gpx():
    data = request.json
    project_id = data.get('project_id')
    filename = data.get('filename')
    file_base64 = data.get('file_base64')

    if not all([project_id, filename, file_base64]):
        return jsonify({'error': '参数不全'}), 400

    if ',' in file_base64: 
        file_base64 = file_base64.split(',')[1]

    # === 核心修改：按项目创建独立文件夹存放 GPX 和 CSV ===
    project_dir = os.path.join(PROJECTS_BASE_DIR, str(project_id))
    os.makedirs(project_dir, exist_ok=True)

    timestamp = int(datetime.now().timestamp())
    safe_name = filename.replace(" ", "_")
    
    # 路径落在对应的项目文件夹内
    gpx_path = os.path.join(project_dir, f"{timestamp}_{safe_name}")
    csv_path = os.path.join(project_dir, f"{timestamp}_{safe_name}.csv")

    try:
        with open(gpx_path, 'wb') as f:
            f.write(base64.b64decode(file_base64))
    except Exception as e:
        return jsonify({'error': f'写入失败: {str(e)}'}), 500

    if parse_gpx_to_csv(gpx_path, csv_path):
        with get_db() as conn:
            conn.execute(
                "INSERT INTO gpx_records (project_id, original_name, gpx_path, csv_path) VALUES (?, ?, ?, ?)",
                (project_id, filename, gpx_path, csv_path)
            )
        return jsonify({'message': 'OK'})
    else:
        if os.path.exists(gpx_path): os.remove(gpx_path)
        if os.path.exists(csv_path): os.remove(csv_path)
        return jsonify({'error': '格式不正确'}), 500

# 将这个路由加到 hike_app.py 中
@hike_bp.route('/api/csv/<int:record_id>', methods=['GET'])
def get_csv_file(record_id):
    with get_db() as conn:
        record = conn.execute("SELECT csv_path FROM gpx_records WHERE id = ?", (record_id,)).fetchone()
    
    if record and os.path.exists(record['csv_path']):
        return send_file(record['csv_path'], mimetype='text/csv')
    return jsonify({'error': 'CSV 文件不存在'}), 404

@hike_bp.route('/api/predict', methods=['POST'])
def predict_time():
    data = request.json
    project_id = data.get('project_id')
    current_dist = float(data.get('current_distance', 0))
    current_time = float(data.get('current_time', 0))

    if current_dist <= 0 or current_time <= 0: return jsonify({'error': '数据需大于0'}), 400

    with get_db() as conn:
        records = conn.execute("SELECT original_name, csv_path FROM gpx_records WHERE project_id = ?", (project_id,)).fetchall()
    
    if not records: return jsonify({'error': '该项目暂无参考轨迹'}), 400

    dfs = []
    for row in records:
        if os.path.exists(row['csv_path']):
            dfs.append({
                'name': row['original_name'], 
                'df': pd.read_csv(row['csv_path'])
            })

    predicted_rem, matched_name = predict_remaining_time(dfs, current_dist, current_time)
    
    if predicted_rem == 0.0:
        return jsonify({'error': '当前距离超出历史轨迹总长，或起点偏差过大'}), 400

    return jsonify({
        'predicted_remaining_hours': round(predicted_rem, 2),
        'total_estimated_hours': round(current_time + predicted_rem, 2),
        'reference_gpx': matched_name
    }) 


# =====================================================================
# 以下为一次性 CSV 历史数据批量重建脚本
# 作用：读取现有数据库记录，利用最新的 parse_gpx_to_csv 覆盖旧版 CSV 文件
# =====================================================================

def _once_batch_regenerate_csv():
    print("\n>>> 开始执行一次性 CSV 重建任务...")
    
    if not os.path.exists(DB_FILE):
        print(f"错误: 找不到数据库文件 {DB_FILE}")
        return

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id, original_name, gpx_path, csv_path FROM gpx_records")
        records = cursor.fetchall()
    except Exception as e:
        print(f"读取数据库失败（可能尚未初始化表）: {e}")
        conn.close()
        return
    
    if not records:
        print("数据库中没有 GPX 记录，无需处理。")
        conn.close()
        return

    success_count = 0
    fail_count = 0

    for row in records:
        record_id = row['id']
        name = row['original_name']
        gpx_path = row['gpx_path']
        csv_path = row['csv_path']

        print(f"正在处理 [{record_id}] {name} ...", end=" ")

        if not os.path.exists(gpx_path):
            print("失败: 原始 GPX 文件丢失。")
            fail_count += 1
            continue

        # 直接调用当前文件内已定义的最新的解析函数
        if parse_gpx_to_csv(gpx_path, csv_path):
            print("成功。")
            success_count += 1
        else:
            print("失败: 解析异常。")
            fail_count += 1

    conn.close()
    print("-" * 30)
    print(f"批量处理完成！成功覆盖: {success_count} 个，失败: {fail_count} 个。")
    print(">>> 任务结束。请手动注释下方的方法调用代码！\n")


# ---------------------------------------------------------------------
# 【执行开关】
# 取消下方代码的注释，保存文件后 Flask 会在加载路由前自动执行一次。
# 确认控制台打印“批量处理完成”后，必须立即重新注释此行，避免每次重启都重复解析！
# ---------------------------------------------------------------------

# _once_batch_regenerate_csv()
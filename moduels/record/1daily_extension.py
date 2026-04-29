import os
import sqlite3
import csv
import io
from datetime import datetime
import pytz
from flask import Blueprint, render_template, request, jsonify, Response

# ==========================================
# 数据库路径与基础配置
# ==========================================
 
DB_FILE = r"db/universal_stats.db"

stats_bp = Blueprint('stats', __name__)

last_add_time = ""

def get_db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS universal_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                record_time TEXT NOT NULL,
                val1 REAL,       
                val2 REAL,       
                remark TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cat_time ON universal_records(category, record_time)')

init_db()

# ==========================================
# 核心数据操作层
# ==========================================
def add_record(project, addtime, count=None, remark_extra=""):
    global last_add_time
    if last_add_time == addtime:
        return

    with get_db_conn() as conn:
        r = conn.execute(
            "SELECT record_time FROM universal_records "
            "WHERE category='daily_stats' AND remark LIKE '%auto%' "
            "ORDER BY record_time DESC LIMIT 1"
        ).fetchone()
        
        if r:
            last_ts = datetime.strptime(r['record_time'], '%Y-%m-%d %H:%M:%S').timestamp()
            curr_ts = datetime.strptime(addtime, '%Y-%m-%d %H:%M:%S').timestamp()
            if (curr_ts - last_ts) < 10:
                return
        
        final_remark = f"{project} | {remark_extra}" if remark_extra else project
        conn.execute(
            "INSERT INTO universal_records (category, record_time, val1, remark) VALUES (?, ?, ?, ?)",
            ('daily_stats', addtime, count, final_remark)
        )
        last_add_time = addtime

def get_stats():
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT id, record_time, val1 as count, remark "
            "FROM universal_records WHERE category='daily_stats' ORDER BY record_time DESC"
        ).fetchall()

    stats = []
    for row in rows:
        time_str = row['record_time']
        count_val = int(row['count']) if row['count'] is not None else ""
        
        full_remark = row['remark'] or ""
        if " | " in full_remark:
            project, actual_remark = full_remark.split(" | ", 1)
        else:
            project, actual_remark = full_remark, ""

        stats.append({
            "id": row['id'],
            "_time": time_str,
            "date": time_str[:10],
            "time": time_str[11:],
            "project": project,
            "count": count_val,
            "remark": actual_remark
        })
    return stats

def delete_record(record_id):
    with get_db_conn() as conn:
        cursor = conn.execute("DELETE FROM universal_records WHERE id=? AND category='daily_stats'", (record_id,))
        return cursor.rowcount > 0

# ==========================================
# 路由层 (API & Views)
# ==========================================
@stats_bp.route('/daily_stats')
def daily_stats_page():
    return render_template('daily_stats.html')

@stats_bp.route('/api/daily_stats', methods=['GET', 'POST'])
def api_daily_stats():
    if request.method == 'POST':
        data = request.get_json()
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        time_str = now.strftime('%Y-%m-%d %H:%M:%S')
        
        add_record(
            project=data.get('project', '默认项目'), 
            addtime=time_str,
            count=data.get('count'),
            remark_extra=data.get('remark', '')
        )
        return jsonify({'status': 'ok'})
        
    return jsonify(get_stats())

@stats_bp.route('/api/daily_stats/<int:record_id>', methods=['DELETE'])
def delete_daily_record(record_id):
    success = delete_record(record_id)
    return jsonify({'status': 'ok', 'deleted': success})

# ==========================================
# CSV 导入导出支持 (Import/Export)
# ==========================================
@stats_bp.route('/api/daily_stats/export', methods=['GET'])
def export_csv():
    stats = get_stats()
    si = io.StringIO()
    cw = csv.writer(si)
    # 写入表头
    cw.writerow(['Time', 'Project', 'Count', 'Remark'])
    
    # 写入数据（按时间正序排列以符合常理）
    stats_asc = sorted(stats, key=lambda x: x['_time'])
    for item in stats_asc:
        cw.writerow([item['_time'], item['project'], item['count'], item['remark']])

    output = si.getvalue()
    # 添加 BOM 防止 Excel 打开乱码
    output_with_bom = '\ufeff' + output 
    return Response(
        output_with_bom,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=daily_stats.csv"}
    )

@stats_bp.route('/api/daily_stats/import', methods=['POST'])
def import_csv():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    
    blob = file.stream.read()
    try:
        raw_content = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            raw_content = blob.decode("gbk")
        except UnicodeDecodeError:
            return jsonify({'error': '文件编码错误'}), 400

    first_line = raw_content.split('\n')[0]
    delimiter = '\t' if '\t' in first_line else ','
    
    stream = io.StringIO(raw_content)
    csv_input = csv.DictReader(stream, delimiter=delimiter)
    
    success_count = 0
    with get_db_conn() as conn:
        for row in csv_input:
            try:
                def get_val(keys):
                    for k in keys:
                        if k in row: return row[k]
                    return None

                t_val = get_val(['Time', 'time', '时间'])
                p_val = get_val(['Project', 'project', '项目'])
                c_val = get_val(['Count', 'count', '次数'])
                r_val = get_val(['Remark', 'remark', '备注']) or ""

                if not t_val or not p_val: continue
                
                # 规范化时间
                raw_time = t_val.replace('/', '-').strip()
                if len(raw_time.split(':')) == 2: raw_time += ":00"
                
                count_num = float(c_val) if c_val else None
                final_remark = f"{p_val} | {r_val}" if r_val else p_val
                
                conn.execute(
                    "INSERT INTO universal_records (category, record_time, val1, remark) VALUES (?, ?, ?, ?)",
                    ('daily_stats', raw_time, count_num, final_remark)
                )
                success_count += 1
            except Exception as e:
                print(f"解析错误: {e}")
                continue

    return jsonify({'status': 'ok', 'imported_rows': success_count})
import sqlite3
import csv
import io,os
from datetime import datetime
import pytz
from flask import Blueprint, render_template, request, jsonify, Response

# 建议在外部配置中传入，这里为了保持独立性写在文件内
DB_FILE = r"db/universal_stats.db"
print("【当前数据库绝对路径是】:", os.path.abspath(DB_FILE))
fuel_bp = Blueprint('fuel', __name__)

# ==========================================
# 数据库通用基础层 (Database Layer)
# ==========================================

def get_db_conn():
    """获取数据库连接并设置 row_factory 以支持字典式访问"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn
 
def add_universal_record(category, val1, val2=None, remark="", record_time=None):
    """通用单行插入函数"""
    if not record_time: 
        tz = pytz.timezone('Asia/Shanghai')
        record_time = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')

    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO universal_records (category, record_time, val1, val2, remark) VALUES (?, ?, ?, ?, ?)",
            (category, record_time, val1, val2, remark)
        )

# ==========================================
# 业务逻辑层：油耗专属处理 (Business Layer)
# ==========================================

def get_fuel_stats():
    """提取 'fuel' 分类的数据，并进行行业标准的油耗逻辑计算"""
    with get_db_conn() as conn:
        # val1 代表 fuel_amount, val2 代表 total_mileage
        rows = conn.execute(
            "SELECT id, record_time, val1 as fuel_amount, val2 as total_mileage, remark "
            "FROM universal_records WHERE category = 'fuel' ORDER BY record_time ASC"
        ).fetchall()

    stats = []
    prev_mileage = None
    prev_date = None

    for row in rows:
        total_mileage = row['total_mileage']
        fuel_amount = row['fuel_amount']
        time_str = row['record_time']
        
        mileage_diff = total_mileage - prev_mileage if prev_mileage is not None else 0
        fuel_efficiency = (fuel_amount / mileage_diff * 100) if mileage_diff > 0 else 0
        
        current_date = datetime.strptime(time_str[:10], '%Y-%m-%d').date()
        days_diff = (current_date - prev_date).days if prev_date is not None else 0
        daily_distance = mileage_diff / days_diff if days_diff > 0 else 0
        
        stats.append({
            "id": row['id'],
            "date": time_str[:10],
            "time": time_str[11:],
            "total_mileage": total_mileage,
            "fuel_amount": fuel_amount,
            "mileage_diff": mileage_diff,
            "fuel_efficiency": round(fuel_efficiency, 2),
            "remark": row['remark'],
            "mileage_range": f"{mileage_diff}km",
            "days_diff": days_diff,
            "daily_distance": round(daily_distance, 1)
        })
        prev_mileage = total_mileage
        prev_date = current_date

    return stats

# ==========================================
# 路由层 (API & Views)
# ==========================================

@fuel_bp.route('/fuel_stats')
def fuel_stats_page():
    return render_template('fuel_stats.html')

@fuel_bp.route('/api/fuel_stats', methods=['GET', 'POST'])
def api_fuel_stats():
    if request.method == 'POST':
        data = request.get_json()
        tz = pytz.timezone('Asia/Shanghai')
        # 统一存入 YYYY-MM-DD HH:MM:SS 格式
        now = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
        with get_db_conn() as conn:
            conn.execute(
                "INSERT INTO universal_records (category, record_time, val1, val2, remark) VALUES (?, ?, ?, ?, ?)",
                ('fuel', now, float(data.get('fuel_amount', 0)), float(data.get('total_mileage', 0)), data.get('remark', ''))
            )
        return jsonify({'status': 'ok'})
    
    # 后端只提供基础数据，包含 ID
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT id, record_time as _time, val1 as fuel_amount, val2 as total_mileage, remark "
            "FROM universal_records WHERE category = 'fuel' ORDER BY _time ASC"
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@fuel_bp.route('/api/fuel_stats/<int:record_id>', methods=['DELETE'])
def delete_fuel_record(record_id):
    with get_db_conn() as conn:
        cursor = conn.execute("DELETE FROM universal_records WHERE id=? AND category='fuel'", (record_id,))
        success = cursor.rowcount > 0
    return jsonify({'status': 'ok', 'deleted': success})

# ==========================================
# CSV 导入导出支持 (Import/Export)
# ==========================================

@fuel_bp.route('/api/fuel_stats/export', methods=['GET'])
def export_csv():
    """导出所有油耗记录为 CSV"""
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT record_time, val1, val2, remark FROM universal_records WHERE category='fuel' ORDER BY record_time ASC"
        ).fetchall()

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['Time', 'Fuel_Amount', 'Total_Mileage', 'Remark'])
    
    for row in rows:
        cw.writerow([row['record_time'], row['val1'], row['val2'], row['remark']])

    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=fuel_stats.csv"}
    )

@fuel_bp.route('/api/fuel_stats/import', methods=['POST'])
def import_csv():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    
    # 1. 读取原始二进制数据
    blob = file.stream.read()
    
    # 2. 尝试多种编码解析
    try:
        # 优先尝试 UTF-8 (带 BOM)
        raw_content = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            # 失败后尝试 GBK (Windows Excel 默认中文编码)
            raw_content = blob.decode("gbk")
        except UnicodeDecodeError:
            return jsonify({'error': '文件编码不是 UTF-8 或 GBK，请检查文件格式'}), 400

    # 3. 识别分隔符 (制表符 \t 或 逗号 ,)
    first_line = raw_content.split('\n')[0]
    delimiter = '\t' if '\t' in first_line else ','
    
    stream = io.StringIO(raw_content)
    csv_input = csv.DictReader(stream, delimiter=delimiter)
    
    success_count = 0
    with get_db_conn() as conn:
        for row in csv_input:
            try:
                # 兼容不同表头大小写的情况
                def get_val(keys):
                    for k in keys:
                        if k in row: return row[k]
                    return None

                # 匹配：Time, Fuel_Amount, Total_Mileage
                t_val = get_val(['Time', 'time'])
                f_val = get_val(['Fuel_Amount', 'fuel_amount', 'Fuel_amount'])
                m_val = get_val(['Total_Mileage', 'total_mileage', 'Total_mileage'])
                r_val = get_val(['Remark', 'remark']) or ""

                if not t_val or not f_val or not m_val: continue

                # 规范化时间
                raw_time = t_val.replace('/', '-').strip()
                if len(raw_time.split(':')) == 2: raw_time += ":00"
                
                conn.execute(
                    "INSERT INTO universal_records (category, record_time, val1, val2, remark) VALUES (?, ?, ?, ?, ?)",
                    ('fuel', raw_time, float(f_val), float(m_val), r_val)
                )
                success_count += 1
            except Exception as e:
                print(f"行解析错误: {e}")
                continue
                
    return jsonify({'status': 'ok', 'imported_rows': success_count})
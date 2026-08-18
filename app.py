from flask import Flask, request, jsonify, send_from_directory, redirect, make_response, Blueprint
import sqlite3, time
import os,re
import json
import importlib
from dotenv import load_dotenv

# ================= 1. 初始化与配置 =================
load_dotenv()

app = Flask(__name__)
DB_PATH = 'db/universal_data.db'
DBstat_FILE = r"db/universal_stats.db"
PAGES_DIR = 'pages'
ACCESS_CODE = os.environ.get('ACCESS_CODE') or "8888"


# ================= 2. 数据库模块 =================
def init_db():
    # 统计数据库
    with sqlite3.connect(DBstat_FILE) as conn:
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

    # 主数据库
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                payload TEXT NOT NULL,
                create_time DATETIME DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS kv_store (
                k TEXT PRIMARY KEY, 
                v TEXT NOT NULL, 
                expire_at REAL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS song_tags (
                song_name TEXT PRIMARY KEY,
                tags TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS play_stats (
                song_name TEXT PRIMARY KEY,
                accumulated_time REAL DEFAULT 0,
                recent_skip_count INTEGER DEFAULT 0,
                last_played_at INTEGER DEFAULT 0
            )
        ''')


# ================= 3. 蓝图与扩展加载 =================
def load_extensions(app):
    """
    扫描目录动态导入扩展，汇总加载结果以避免控制台刷屏。
    """
    current_dir = 'moduels'
    loaded_blueprints = []
    warnings = []
    errors = []
    abs_current_dir = os.path.abspath(current_dir)
    if os.path.exists(current_dir):
        for root, dirs, files in os.walk(current_dir):
            for filename in files:
                if filename.endswith('_extension.py'):
                    file_path = os.path.join(root, filename)
                    # 获取相对于 current_dir 的相对路径 (例如: subfolder/news_extension.py)
                    relative_path = os.path.relpath(file_path, abs_current_dir)
                    
                    # 将路径转换为 Python 模块导入路径
                    # 去掉 .py 后缀，并将系统路径分隔符（\ 或 /）替换为 Python 包所需的点号 (.)
                    module_name = relative_path[:-3].replace(os.sep, '.')
                    try:
                        module = importlib.import_module(current_dir + "." + module_name)
                        blueprint_found = False
                        
                        for attr_name in dir(module):
                            attr = getattr(module, attr_name)
                            if isinstance(attr, Blueprint):
                                app.register_blueprint(attr)
                                loaded_blueprints.append(attr.name)
                                blueprint_found = True
                        
                        if not blueprint_found:
                            warnings.append(filename)
                    except Exception as e:
                        errors.append(f"{filename} ({str(e)})")
                
            

    # 手动挂载历史遗留模块
    try:
        from aiocr import manuals_bp
        app.register_blueprint(manuals_bp)
        loaded_blueprints.append("manuals_bp(aiocr)")
    except ImportError:
        pass

    # 汇总输出 UI 显示
    print(f"[*] 蓝图模块加载完成 | 总计成功: {len(loaded_blueprints)} 个")
    if loaded_blueprints:
        print(f"    - 已挂载: {', '.join(loaded_blueprints)}")
    if warnings:
        print(f"[!] 警告: {len(warnings)} 个文件未找到 Blueprint 实例 ({', '.join(warnings)})")
    if errors:
        print(f"[x] 错误: {len(errors)} 个模块挂载失败 ({', '.join(errors)})")

load_extensions(app)


# ================= 4. 中间件与鉴权 =================
@app.before_request
def check_access():
    if request.path == '/login' or request.path.startswith('/static/') or request.path == '/favicon.ico':
        return
        
    if request.cookies.get('access_token') == ACCESS_CODE or 'static' in request.path or 'skip' in request.path:
        return
        
    if request.method == 'POST':
        return

    if request.path.startswith('/api/'):
        return jsonify({"error": "Unauthorized"}), 403
    return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        code = request.form.get('code')
        if code == ACCESS_CODE:
            resp = make_response(redirect('/'))
            resp.set_cookie('access_token', code, max_age=30*24*3600)
            return resp
        return "<h1>访问码错误</h1><a href='/login'>返回重试</a>", 403
    
    return '''
    <div style="text-align:center; margin-top: 100px; font-family: sans-serif;">
        <h2>🔒 请输入访问码</h2>
        <form method="post">
            <input type="password" name="code" style="padding: 10px; font-size: 16px;" autofocus />
            <button type="submit" style="padding: 10px 20px; font-size: 16px;">进入</button>
        </form>
    </div>
    '''


# ================= 5. 核心 API 接口 =================
import requests
from datetime import datetime
@app.route('/api2/oil/minline', methods=['GET'])
def get_oil_summary():
    """
    获取国际原油当日关键价格摘要（基于新浪分时数据）
    返回：最新价、均价、最高、最低、开盘、昨收等
    """
    sina_url = (
        "https://stock2.finance.sina.com.cn/futures/api/openapi.php/"
        "GlobalFuturesService.getGlobalFuturesMinLine"
        "?symbol=OIL&callback=var%20t1hf_OIL="
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://finance.sina.com.cn/futures/quotes/OIL.shtml",
    }

    try:
        resp = requests.get(sina_url, headers=headers, timeout=10)
        resp.raise_for_status()
        raw_text = resp.text

        # 提取 JSONP 中的 JSON 对象
        match = re.search(r'\(\s*({.*?})\s*\)', raw_text, re.DOTALL)
        if not match:
            return jsonify({"error": "无法解析新浪返回数据"}), 502

        data = json.loads(match.group(1))
        minline = data.get("result", {}).get("data", {}).get("minLine_1d", [])

        if not minline:
            return jsonify({"error": "无分时数据"}), 502

        # 第一条数据通常包含日期和开盘信息
        first = minline[0]
        last = minline[-1]

        # 提取价格序列（每分钟的最新价，在第一个字段后的第1个位置）
        # 注意：第一个元素格式与后面不同
        prices = []
        for point in minline:
            try:
                # 跳过第一条的日期部分，取价格（索引1）
                price = float(point[1])
                prices.append(price)
            except (ValueError, IndexError):
                continue

        if not prices:
            return jsonify({"error": "无法提取价格数据"}), 502

        trade_date = first[0] if len(first) > 0 else ""
        open_price = float(first[5]) if len(first) > 5 else prices[0]
        latest_price = prices[-1]
        avg_price = round(sum(prices) / len(prices), 3)
        high_price = max(prices)
        low_price = min(prices)
        yesterday_close = float(first[1]) if len(first) > 1 else None
        change_pct = round((latest_price - yesterday_close) / yesterday_close * 100, 2) if yesterday_close else None

        return jsonify({
            "code": 0,
            "data": {
                "trade_date": trade_date,
                "latest_price": latest_price,
                "avg_price": avg_price,
                "high_price": high_price,
                "low_price": low_price,
                "open_price": open_price,
                "yesterday_close": yesterday_close,
                "change_pct": change_pct,
                "data_count": len(prices)
            },
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    except requests.exceptions.Timeout:
        return jsonify({"error": "请求新浪接口超时"}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"请求失败: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"数据处理异常: {str(e)}"}), 500

import requests
import json
import re
from datetime import datetime, timedelta

@app.route('/api2/oil/domestic/latest', methods=['GET'])
def get_chongqing_oil():
    """
    获取重庆最新油价（绕过 filter 解析风险）
    """
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPTA_WEB_YJ_JH",
        "columns": "ALL",
        # 不传 filter，只排序和分页
        "sortColumns": "DIM_DATE",
        "sortTypes": "-1",
        "pageNumber": "1",
        "pageSize": "50",  # 足够覆盖所有省份
        "source": "WEB",
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://data.eastmoney.com/",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        raw_text = resp.text

        # 提取 JSON（处理可能的 JSONP）
        json_str = raw_text
        match = re.search(r'(\{.*\})', raw_text, re.DOTALL)
        if match:
            json_str = match.group(1)

        data = json.loads(json_str)

        if not data.get('success'):
            return jsonify({"code": -1, "msg": data.get('message', '接口失败')}), 502

        items = data.get('result', {}).get('data', [])
        if not items:
            return jsonify({"code": -1, "msg": "暂无数据"}), 404

        # 筛选重庆
        cq = None
        for item in items:
            if item.get('CITYNAME') == '重庆':
                cq = item
                break

        if not cq:
            return jsonify({"code": -1, "msg": "未找到重庆数据"}), 404

        return jsonify({
            "code": 0,
            "data": {
                "city": "重庆",
                "date": cq.get('DIM_DATE', '')[:10],
                "gasoline_92": float(cq.get('V92', 0)),
                "gasoline_95": float(cq.get('V95', 0)),
                "gasoline_89": float(cq.get('V89', 0)),
            }
        })

    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500


 
@app.route('/api2/<collection>', methods=['POST'])
def save_data(collection):
    data = request.json
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO store (collection, payload) VALUES (?, ?)', 
                     (collection, json.dumps(data, ensure_ascii=False)))
    return jsonify({"status": "success"})

@app.route('/api2/<collection>', methods=['GET'])
def get_data(collection):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT payload, create_time FROM store WHERE collection=? ORDER BY id DESC LIMIT 50', (collection,))
        rows = cursor.fetchall()
    
    result = []
    for row in rows:
        item = json.loads(row[0])
        item['_time'] = row[1]
        result.append(item)
    return jsonify(result)

@app.route('/api2/kv/<key>', methods=['POST'])
def set_kv(key):
    data = request.json
    payload = json.dumps(data.get('payload', {}), ensure_ascii=False)
    expire_at = data.get('expire_at') 
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR REPLACE INTO kv_store (k, v, expire_at) VALUES (?, ?, ?)', 
                     (key, payload, expire_at))
    return jsonify({"status": "success"})

@app.route('/api2/kv/<key>', methods=['GET'])
def get_kv(key):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT v, expire_at FROM kv_store WHERE k=?', (key,))
        row = cursor.fetchone()
        
        if row:
            v, expire_at = row
            if expire_at and time.time() > expire_at:
                conn.execute('DELETE FROM kv_store WHERE k=?', (key,))
                return jsonify({"error": "提取码已过期，数据已永久销毁"}), 404
            return jsonify(json.loads(v))
        return jsonify({"error": "提取码不存在或已被销毁"}), 404
 
# ================= 6. 静态页面与路由 =================
# ================= 修改 1：get_html_path =================
def get_html_path(filename):
    """在 PAGES_DIR 中查找 HTML 文件路径"""
    # 1. 目录映射（去掉 .html 后作为目录）
    candidate_dir = filename[:-5] if filename.endswith('.html') else filename
    dir_index = os.path.join(PAGES_DIR, candidate_dir, 'index.html')
    if os.path.exists(dir_index):
        return dir_index

    # 2. 精确文件路径
    exact_path = os.path.join(PAGES_DIR, filename)
    if os.path.exists(exact_path):
        return exact_path

    # 3. 递归查找（根据文件名）
    base_filename = os.path.basename(filename)
    for root, dirs, files in os.walk(PAGES_DIR):
        if base_filename in files:
            return os.path.join(root, base_filename)

    return None

# ================= 修改 2：页面列表 API =================
@app.route('/api2/_sys/pages', methods=['GET'])
def get_pages_list():
    if not os.path.exists(PAGES_DIR):
        return jsonify([])

    items = []

    for root, dirs, files in os.walk(PAGES_DIR):
        rel_dir = os.path.relpath(root, PAGES_DIR)
        if rel_dir == '.':
            rel_dir = ''

        # 如果当前目录有 index.html，并且不是根目录，则先添加目录入口
        if 'index.html' in files and rel_dir:
            dir_name = os.path.basename(rel_dir)
            if '-' not in dir_name:
                index_file = os.path.join(root, 'index.html')
                items.append((rel_dir, index_file))

        # 同时遍历当前目录下的普通 .html 文件（排除 index.html）
        for f in files:
            if f.endswith('.html') and f != 'index.html':
                file_base = f[:-5]  # 去掉 .html
                if '-' in file_base:
                    continue

                if rel_dir:
                    page_path = os.path.join(rel_dir, file_base).replace(os.sep, '/')
                else:
                    page_path = file_base

                full_path = os.path.join(root, f)
                items.append((page_path, full_path))

    # 简单去重，避免目录入口和同名文件路径冲突
    seen = set()
    unique_items = []
    for path, full_path in items:
        if path not in seen:
            seen.add(path)
            unique_items.append((path, full_path))
    items = unique_items

    # 按文件修改时间降序排序
    items.sort(key=lambda x: os.path.getmtime(x[1]), reverse=True)

    return jsonify([item[0] for item in items])

# ================= 修改 3：serve_html_with_icon =================
def serve_html_with_icon(filename):
    if not filename or not isinstance(filename, str):
        app.logger.error("无效的文件名: %s", filename)
        return "Invalid filename", 400

    if not filename.endswith('.html'):
        filename += '.html'

    # 获取文件真实路径（支持目录映射）
    html_path = get_html_path(filename)

    if not html_path or not os.path.exists(html_path):
        return "Page Not Found", 404

    # 提取用于查找 SVG 的基础名
    base_name = os.path.basename(html_path).replace('.html', '')

    if base_name == 'index':
        parent_dir = os.path.dirname(html_path)
        if parent_dir == PAGES_DIR:
            # 根目录 index.html 使用 "index" 作为图标名
            icon_base = base_name
        else:
            # 子目录 index.html 使用父目录名作为图标名（如 project1）
            icon_base = os.path.basename(parent_dir)
        # 移除可能的数字前缀（与原有逻辑保持一致）
        main_name2 = re.sub(r'^\d+', '', icon_base).lower()
    else:
        # 普通文件：保留原有逻辑（处理连字符等）
        main_name = base_name.split('-')[0].lower() if '-' in base_name else base_name.lower()
        main_name2 = re.sub(r'^\d+', '', main_name)

    svg_path = os.path.join('static', 'svg', f'{main_name2}.svg')

    # 如果存在对应的 SVG 图标，注入到 HTML <head> 中
    if os.path.exists(svg_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()

        icon_tag = f'<link rel="icon" href="/static/svg/{main_name2}.svg" type="image/svg+xml">'
        if '</head>' in content:
            content = content.replace('</head>', f'    {icon_tag}\n</head>', 1)
        else:
            content = icon_tag + '\n' + content
        return content

    # 无 SVG 时直接返回文件
    directory = os.path.dirname(html_path)
    file_name = os.path.basename(html_path)
    return send_from_directory(directory, file_name)

@app.route('/x')
def index():
    return serve_html_with_icon('index.html')

@app.route('/<path:filename>')
def serve_pages(filename):
    return serve_html_with_icon(filename)
    
# ================= 7. 启动入口 =================
if __name__ == '__main__':
    os.makedirs(PAGES_DIR, exist_ok=True)
    init_db()
    app.run(host='0.0.0.0', port=8100, debug=True)
    print('https://apple.su7.dpdns.org')
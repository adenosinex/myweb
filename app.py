from flask import Flask, request, jsonify, send_from_directory, redirect, make_response, Blueprint
import sqlite3, time
import os
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

    if os.path.exists(current_dir):
        for filename in os.listdir(current_dir):
            if filename.endswith('_extension.py'):
                module_name = filename[:-3]
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
        
    if request.cookies.get('access_token') == ACCESS_CODE or 'skip' in request.path:
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
@app.route('/api/_sys/pages', methods=['GET'])
def get_pages_list():
    if not os.path.exists(PAGES_DIR):
        return jsonify([])
    files = [f for f in os.listdir(PAGES_DIR) if f.endswith('.html') and f != 'index.html']
    files = [f for f in files if not '-' in f]
    return jsonify([f.replace('.html', '') for f in files])

@app.route('/api/<collection>', methods=['POST'])
def save_data(collection):
    data = request.json
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO store (collection, payload) VALUES (?, ?)', 
                     (collection, json.dumps(data, ensure_ascii=False)))
    return jsonify({"status": "success"})

@app.route('/api/<collection>', methods=['GET'])
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

@app.route('/api/kv/<key>', methods=['POST'])
def set_kv(key):
    data = request.json
    payload = json.dumps(data.get('payload', {}), ensure_ascii=False)
    expire_at = data.get('expire_at') 
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR REPLACE INTO kv_store (k, v, expire_at) VALUES (?, ?, ?)', 
                     (key, payload, expire_at))
    return jsonify({"status": "success"})

@app.route('/api/kv/<key>', methods=['GET'])
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
def serve_html_with_icon(filename):
    if not filename.endswith('.html'):
        filename += '.html'
        
    PAGES_DIR2 = PAGES_DIR
    html_path = os.path.join(PAGES_DIR, filename)
    
    if not os.path.exists(html_path):
        PAGES_DIR2 = os.path.join(PAGES_DIR, "second")
        html_path = os.path.join(PAGES_DIR2, filename)
        if not os.path.exists(html_path):
            return "Page not found", 404

    base_name = filename[:-5] 
    base_name = base_name.split('-')[0] if '-' in base_name else base_name
    svg_path = os.path.join('static', "svg", f'{base_name}.svg')

    if os.path.exists(svg_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        icon_tag = f'<link rel="icon" href="/static/svg/{base_name}.svg" type="image/svg+xml">'
        
        if '</head>' in content:
            content = content.replace('</head>', f'    {icon_tag}\n</head>', 1)
        else:
            content = icon_tag + '\n' + content
            
        return content

    return send_from_directory(PAGES_DIR2, filename)

@app.route('/')
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
# app.py
from flask import Flask, request, jsonify, send_from_directory, redirect, make_response
import sqlite3
import os
import json
from dotenv import load_dotenv

load_dotenv()  # 自动寻找当前目录下的 .env 并注入到 os.environ
# ================= 引入外部模块 =================
from player_extension import player_bp  # 导入播放器蓝图
from tags_extension import tags_bp      # 导入AI打标蓝图

app = Flask(__name__)
DB_PATH = 'universal_data.db'
PAGES_DIR = 'pages'
ACCESS_CODE =   os.environ.get('ACCESS_CODE') or "8888"

# 注册外部蓝图路由到当前 App
app.register_blueprint(player_bp)
app.register_blueprint(tags_bp)

# ================= 数据库初始化 =================
def init_db():
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
        # 补全：歌曲标签专属表（供 tags_extension 使用）
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

# ================= 1. 安全拦截器 =================
@app.before_request
def check_access():
    # 加入 /static/ 和 /favicon.ico，让静态资源免登录即可访问
    if request.path == '/login' or request.path.startswith('/api/') or request.path.startswith('/stream/') or request.path.startswith('/static/') or request.path == '/favicon.ico':
        return
    if request.cookies.get('access_token') != ACCESS_CODE:
        return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        code = request.form.get('code')
        if code == ACCESS_CODE:
            # 修改：登录成功后跳转到根目录（导航页）
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

# ================= 2. 系统与万能数据 API =================
# 恢复：自动扫描 pages 目录下的所有网页（供导航页使用）
@app.route('/api/_sys/pages', methods=['GET'])
def get_pages_list():
    if not os.path.exists(PAGES_DIR):
        return jsonify([])
    # 找出所有 html 文件，并排除 index.html 自身
    files = [f for f in os.listdir(PAGES_DIR) if f.endswith('.html') and f != 'index.html']
    # 返回去掉后缀的名称列表
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

# ================= 3. 静态页面路由 =================
@app.route('/')
def index():
    return send_from_directory(PAGES_DIR, 'index.html')

@app.route('/<path:filename>')
def serve_pages(filename):
    if not filename.endswith('.html'):
        filename += '.html'
    return send_from_directory(PAGES_DIR, filename)

if __name__ == '__main__':
    os.makedirs(PAGES_DIR, exist_ok=True)
    init_db()
    app.run(host='0.0.0.0', port=8100, debug=True, ssl_context='adhoc')
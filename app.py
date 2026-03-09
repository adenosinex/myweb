from flask import Flask, request, jsonify, send_from_directory, redirect, make_response, Blueprint
import sqlite3
import os
import json
import importlib
from dotenv import load_dotenv

load_dotenv()  # 自动寻找当前目录下的 .env 并注入到 os.environ

app = Flask(__name__)
DB_PATH = 'universal_data.db'
PAGES_DIR = 'pages'
ACCESS_CODE = os.environ.get('ACCESS_CODE') or "8888"

# ================= 动态加载外部蓝图 (Extensions) =================
def load_extensions(app):
    """
    扫描当前目录，自动导入所有以 _extension.py 结尾的文件，
    并将其中的 Blueprint 实例注册到 app 中。
    """
    current_dir = os.path.dirname(os.path.abspath(__name__))
    
    for filename in os.listdir(current_dir):
        # 匹配后缀为 _extension.py 的文件
        if filename.endswith('_extension.py'):
            module_name = filename[:-3]  # 去掉 .py 后缀
            try:
                # 动态导入模块
                module = importlib.import_module(module_name)
                
                # 遍历模块内的所有对象，寻找 Blueprint 实例
                blueprint_found = False
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, Blueprint):
                        app.register_blueprint(attr)
                        print(f"✅ 已自动挂载蓝图: {attr.name} (来自 {filename})")
                        blueprint_found = True
                
                if not blueprint_found:
                    print(f"⚠️ 警告: 在 {filename} 中未找到可用的 Blueprint 实例。")
                    
            except Exception as e:
                print(f"❌ 挂载蓝图失败 {filename}: {str(e)}")

    # 针对之前未按规范命名的文件（如 aiocr.py），建议将其重命名为 aiocr_extension.py。
    # 如果暂时不方便重命名，可以在下方手动保留它的导入和注册：
    try:
        from aiocr import manuals_bp
        app.register_blueprint(manuals_bp)
        print("✅ 已手动挂载蓝图: manuals_bp (来自 aiocr.py)")
    except ImportError:
        pass # 如果你已经将其重命名为 aiocr_extension.py，这里会自动跳过不报错

# 执行动态加载
load_extensions(app)
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

def serve_html_with_icon(filename):
    """读取 HTML 文件，若存在同名 svg，则向其动态插入 icon 标签"""
    if not filename.endswith('.html'):
        filename += '.html'
        
    html_path = os.path.join(PAGES_DIR, filename)
    if not os.path.exists(html_path):
        return "Page not found", 404

    # 提取基础文件名，例如 'player.html' -> 'player'
    base_name = filename[:-5] 
    svg_path = os.path.join('static', f'{base_name}.svg')

    # 如果 static 目录下存在同名的 svg 图标
    if os.path.exists(svg_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 准备要插入的 link 标签
        icon_tag = f'<link rel="icon" href="/static/{base_name}.svg" type="image/svg+xml">'
        
        # 查找 </head> 并在其前面硬编码插入标签
        if '</head>' in content:
            content = content.replace('</head>', f'    {icon_tag}\n</head>', 1)
        else:
            # 如果极端情况没有 </head> 标签，直接插在最前面
            content = icon_tag + '\n' + content
            
        return content

    # 如果没有对应的 svg，按原样发送文件
    return send_from_directory(PAGES_DIR, filename)

@app.route('/')
def index():
    return serve_html_with_icon('index.html')

@app.route('/<path:filename>')
def serve_pages(filename):
    return serve_html_with_icon(filename)

 

if __name__ == '__main__':
    os.makedirs(PAGES_DIR, exist_ok=True)
    init_db()
    app.run(host='0.0.0.0', port=8100, debug=True, ssl_context='adhoc')
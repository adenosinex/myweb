from flask import Flask, request, jsonify, send_from_directory
import sqlite3
import os
import json

app = Flask(__name__, static_folder='pages')
DB_PATH = 'universal_data.db'
PAGES_DIR = 'pages'

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

# --- 页面路由 ---
@app.route('/')
def index():
    return send_from_directory(PAGES_DIR, 'index.html')

@app.route('/<path:filename>')
def serve_pages(filename):
    if not filename.endswith('.html'):
        filename += '.html'
    return send_from_directory(PAGES_DIR, filename)

# --- 系统接口：自动扫描 pages 目录下的所有网页 ---
@app.route('/api/_sys/pages', methods=['GET'])
def get_pages_list():
    if not os.path.exists(PAGES_DIR):
        return jsonify([])
    # 找出所有 html 文件，并排除 index.html
    files = [f for f in os.listdir(PAGES_DIR) if f.endswith('.html') and f != 'index.html']
    # 返回去掉后缀的名称列表
    return jsonify([f.replace('.html', '') for f in files])

# --- 万能数据接口 (读/写) ---
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

import time # 在文件开头补充引入 time 模块

# 1. 找到 init_db() 函数，在里面新增一张 kv_store 表
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # 原来的 store 表...
        conn.execute('''
            CREATE TABLE IF NOT EXISTS store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                payload TEXT NOT NULL,
                create_time DATETIME DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        # ---> 新增：专门用于文字中转的键值对表，支持过期时间
        conn.execute('''
            CREATE TABLE IF NOT EXISTS kv_store (
                k TEXT PRIMARY KEY, 
                v TEXT NOT NULL, 
                expire_at REAL
            )
        ''')

# 2. 在路由区域，加入这两个 KV 专属接口
@app.route('/api/kv/<key>', methods=['POST'])
def set_kv(key):
    data = request.json
    payload = json.dumps(data.get('payload', {}), ensure_ascii=False)
    expire_at = data.get('expire_at') # 接收 Unix 时间戳（秒），可为 None
    
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
            # 核心安全逻辑：如果设置了过期时间，且当前时间已超过
            if expire_at and time.time() > expire_at:
                conn.execute('DELETE FROM kv_store WHERE k=?', (key,))
                return jsonify({"error": "提取码已过期，数据已永久销毁"}), 404
            
            return jsonify(json.loads(v))
        return jsonify({"error": "提取码不存在或已被销毁"}), 404
        
if __name__ == '__main__':
    os.makedirs(PAGES_DIR, exist_ok=True)
    init_db()
    app.run(host='0.0.0.0', port=8000, debug=True)
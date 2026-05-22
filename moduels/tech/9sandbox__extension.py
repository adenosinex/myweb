import os
import sqlite3
import time
import traceback
from typing import List, Dict, Optional
from flask import Blueprint, request, jsonify, abort

# ================= 基础配置与目录初始化 =================
BASE_DIR = os.path.abspath("db/sandbox")
PAGES_DIR = os.path.join(BASE_DIR, "pages_data")
DB_FILE = os.path.join(BASE_DIR, "sandbox_index.db")

os.makedirs(PAGES_DIR, exist_ok=True)

sandbox_bp = Blueprint('sandbox', __name__,url_prefix='/sandbox')

@sandbox_bp.errorhandler(Exception)
def handle_exception(e):
    traceback.print_exc()
    return jsonify({"status": "error", "message": f"服务器内部错误: {str(e)}"}), 500

# ================= 核心引擎层 =================
class SandboxIndexEngine:
    def __init__(self, pages_dir: str, db_path: str):
        self.pages_dir = pages_dir
        self.db_path = db_path
        self._init_db()
        self._sync_pages()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute('DROP TABLE IF EXISTS pages')
            conn.execute('''
                CREATE TABLE pages (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    icon TEXT,
                    html_content TEXT NOT NULL
                )
            ''')
            conn.commit()

    def _parse_page_file(self, filepath: str) -> Optional[Dict]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().lstrip('\ufeff').strip()
                
            if not content.startswith('---'): return None
            parts = content.split('---', 2)
            if len(parts) < 3: return None
            
            meta = {}
            for line in parts[1].strip().split('\n'):
                line = line.strip()
                if not line or ':' not in line: continue
                key, val = line.split(':', 1)
                meta[key.strip()] = val.strip().strip('"').strip("'")
                
            meta['html_content'] = parts[2].strip()
            return meta
        except Exception as e:
            print(f"解析文件失败 {filepath}: {e}")
            return None

    def _sync_pages(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pages") 
            for filename in os.listdir(self.pages_dir):
                if not filename.endswith('.md'): continue
                meta = self._parse_page_file(os.path.join(self.pages_dir, filename))
                if meta and 'id' in meta:
                    cursor.execute('''
                        INSERT INTO pages (id, title, icon, html_content)
                        VALUES (?, ?, ?, ?)
                    ''', (meta['id'], meta.get('title', '未命名项目'), meta.get('icon', ''), meta.get('html_content', '')))
            conn.commit()

    def get_all_pages(self) -> List[Dict]:
        with self._get_conn() as conn:
            return [dict(row) for row in conn.execute('SELECT id, title, icon FROM pages ORDER BY id DESC').fetchall()]

    def get_page_detail(self, page_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute('SELECT id, title, icon, html_content FROM pages WHERE id = ?', (page_id,)).fetchone()
            return dict(row) if row else None

engine = SandboxIndexEngine(pages_dir=PAGES_DIR, db_path=DB_FILE)

# ================= 数据 API 接口 =================

@sandbox_bp.route('/api/pages', methods=['GET'])
def get_pages():
    """获取列表"""
    return jsonify(engine.get_all_pages())

@sandbox_bp.route('/api/pages', methods=['POST'])
def add_page():
    """新建"""
    data = request.json
    title, icon, html_content = data.get('title'), data.get('icon', ''), data.get('html_content')
    if not title or not html_content: return jsonify({"error": "标题和代码内容不能为空"}), 400
        
    try:
        page_id = f"p_{int(time.time())}"
        md_content = f"---\nid: {page_id}\ntitle: \"{title}\"\nicon: \"{icon}\"\n---\n{html_content}\n"
        with open(os.path.join(PAGES_DIR, f"{page_id}.md"), 'w', encoding='utf-8') as f:
            f.write(md_content)
        engine._sync_pages()
        return jsonify({"status": "success", "id": page_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/api/pages/<page_id>', methods=['GET'])
def get_single_page(page_id):
    """读取单条数据（供编辑页回填）"""
    data = engine.get_page_detail(page_id)
    if not data: return jsonify({"error": "未找到该项目"}), 404
    return jsonify(data)

@sandbox_bp.route('/api/pages/<page_id>', methods=['PUT'])
def update_page(page_id):
    """更新现有数据"""
    data = request.json
    title, icon, html_content = data.get('title'), data.get('icon', ''), data.get('html_content')
    if not title or not html_content: return jsonify({"error": "标题和代码内容不能为空"}), 400

    md_path = os.path.join(PAGES_DIR, f"{page_id}.md")
    if not os.path.exists(md_path): return jsonify({"error": "项目文件不存在"}), 404

    try:
        md_content = f"---\nid: {page_id}\ntitle: \"{title}\"\nicon: \"{icon}\"\n---\n{html_content}\n"
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        engine._sync_pages()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/view/<page_id>')
def view_page(page_id):
    """返回原生 HTML"""
    data = engine.get_page_detail(page_id)
    if data: return data['html_content']
    abort(404)
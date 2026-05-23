import os
import sqlite3
import time
import traceback
import csv
import re
import html
from io import StringIO, BytesIO
from typing import List, Dict, Optional
from flask import Blueprint, request, jsonify, abort, send_file

# ================= 基础配置与目录初始化 =================
BASE_DIR = os.path.abspath("db/sandbox")
PAGES_DIR = os.path.join(BASE_DIR, "pages_data")
DB_FILE = os.path.join(BASE_DIR, "sandbox_index.db")

os.makedirs(PAGES_DIR, exist_ok=True)

sandbox_bp = Blueprint('sandbox', __name__, url_prefix='/sandbox')

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
                    tags TEXT DEFAULT '',
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
                        INSERT INTO pages (id, title, icon, tags, html_content)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        meta['id'], 
                        meta.get('title', '未命名项目'), 
                        meta.get('icon', ''), 
                        meta.get('tags', ''),
                        meta.get('html_content', '')
                    ))
            conn.commit()

    def get_all_pages(self) -> List[Dict]:
        with self._get_conn() as conn:
            return [dict(row) for row in conn.execute('SELECT id, title, icon, tags FROM pages ORDER BY id DESC').fetchall()]

    def get_page_detail(self, page_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute('SELECT id, title, icon, tags, html_content FROM pages WHERE id = ?', (page_id,)).fetchone()
            return dict(row) if row else None

engine = SandboxIndexEngine(pages_dir=PAGES_DIR, db_path=DB_FILE)

# ================= 数据 API 接口 =================

@sandbox_bp.route('/api/pages', methods=['GET'])
def get_pages():
    return jsonify(engine.get_all_pages())

@sandbox_bp.route('/api/pages', methods=['POST'])
def add_page():
    data = request.json
    title = data.get('title')
    icon = data.get('icon', '')
    tags = data.get('tags', '')
    html_content = data.get('html_content')
    
    if not title or not html_content: return jsonify({"error": "标题和代码内容不能为空"}), 400
        
    try:
        page_id = f"p_{int(time.time())}"
        md_content = f"---\nid: {page_id}\ntitle: \"{title}\"\nicon: \"{icon}\"\ntags: \"{tags}\"\n---\n{html_content}\n"
        with open(os.path.join(PAGES_DIR, f"{page_id}.md"), 'w', encoding='utf-8') as f:
            f.write(md_content)
        engine._sync_pages()
        return jsonify({"status": "success", "id": page_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/api/pages/<page_id>', methods=['GET'])
def get_single_page(page_id):
    data = engine.get_page_detail(page_id)
    if not data: return jsonify({"error": "未找到该项目"}), 404
    return jsonify(data)

@sandbox_bp.route('/api/pages/<page_id>', methods=['PUT'])
def update_page(page_id):
    data = request.json
    title = data.get('title')
    icon = data.get('icon', '')
    tags = data.get('tags', '')
    html_content = data.get('html_content')
    
    if not title or not html_content: return jsonify({"error": "标题和代码内容不能为空"}), 400

    md_path = os.path.join(PAGES_DIR, f"{page_id}.md")
    if not os.path.exists(md_path): return jsonify({"error": "项目文件不存在"}), 404

    try:
        md_content = f"---\nid: {page_id}\ntitle: \"{title}\"\nicon: \"{icon}\"\ntags: \"{tags}\"\n---\n{html_content}\n"
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        engine._sync_pages()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/api/export_untagged', methods=['GET'])
def export_untagged():
    """导出未标记的项目，提取纯文本前100字符作为预览"""
    with engine._get_conn() as conn:
        rows = conn.execute("SELECT id, title, html_content FROM pages WHERE tags IS NULL OR tags = ''").fetchall()
    
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['id', 'title', 'tags', 'content_snippet'])
    
    for r in rows:
        raw_html = r['html_content']
        
        # 1. 过滤掉 <script> 和 <style> 标签及其内部的所有内容，防止代码文本混入预览
        clean_text = re.sub(r'<(script|style).*?>.*?</\1>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
        
        # 2. 去除所有其他的 HTML 标签
        clean_text = re.sub(r'<[^>]+>', '', clean_text)
        
        # 3. 还原 HTML 实体字符（例如将 &nbsp; 变为空格，&lt; 变为 <）
        clean_text = html.unescape(clean_text)
        
        # 4. 清理多余的换行符和首尾空格，截取前 100 个字符
        snippet = clean_text.replace('\n', ' ').replace('\r', '')
        snippet = re.sub(r'\s+', ' ', snippet).strip()[:100]
        
        cw.writerow([r['id'], r['title'], '', snippet])
        
    output = BytesIO(si.getvalue().encode('utf-8-sig'))
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name='untagged_pages.csv')

@sandbox_bp.route('/api/import_tags', methods=['POST'])
def import_tags():
    if 'file' not in request.files: return jsonify({"error": "没有文件部分"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "未选择文件"}), 400

    try:
        stream = StringIO(file.stream.read().decode('utf-8-sig'))
        csv_reader = csv.DictReader(stream)
        
        updated_count = 0
        for row in csv_reader:
            page_id = row.get('id', '').strip()
            new_tags = row.get('tags', '').strip()
            
            if page_id and new_tags:
                md_path = os.path.join(PAGES_DIR, f"{page_id}.md")
                if os.path.exists(md_path):
                    meta = engine._parse_page_file(md_path)
                    if meta:
                        md_content = f"---\nid: {meta['id']}\ntitle: \"{meta['title']}\"\nicon: \"{meta.get('icon','')}\"\ntags: \"{new_tags}\"\n---\n{meta['html_content']}\n"
                        with open(md_path, 'w', encoding='utf-8') as f:
                            f.write(md_content)
                        updated_count += 1
                        
        engine._sync_pages()
        return jsonify({"status": "success", "updated": updated_count})
    except Exception as e:
        return jsonify({"error": f"CSV解析错误: {str(e)}"}), 500

@sandbox_bp.route('/view/<page_id>')
def view_page(page_id):
    data = engine.get_page_detail(page_id)
    if data: return data['html_content']
    abort(404)
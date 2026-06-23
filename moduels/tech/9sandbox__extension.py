import os
import sqlite3
import time
import traceback
import csv
import re
import html
import json
from io import StringIO, BytesIO
from typing import List, Dict, Optional
from flask import Blueprint, request, jsonify, abort, send_file

# ================= 基础配置与目录初始化 =================
BASE_DIR = os.path.abspath("db/sandbox")
PAGES_DIR = os.path.join(BASE_DIR, "pages_data")
DB_FILE = os.path.join(BASE_DIR, "sandbox_index.db")
ORDER_FILE = os.path.join(BASE_DIR, "layout_order.json")

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
        self._sanitize_directories() # 启动时自动修复包含逗号的错误文件夹名
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
                    folder TEXT DEFAULT '未分类',
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

    def _get_page_path(self, page_id: str) -> Optional[str]:
        for root, dirs, files in os.walk(self.pages_dir):
            if f"{page_id}.md" in files:
                return os.path.join(root, f"{page_id}.md")
        return None

    def _sanitize_directories(self):
        """修复因为旧版CSV导入导致的带有逗号的错误文件夹名，只取第一个标签作为物理分类"""
        for item in os.listdir(self.pages_dir):
            item_path = os.path.join(self.pages_dir, item)
            if not os.path.isdir(item_path) or item == '未分类':
                continue
                
            if ',' in item or '，' in item:
                # 取逗号前的第一个词作为标准的文件夹名
                clean_name = re.split(r'[,，]', item)[0].strip()
                if not clean_name: clean_name = '未分类'
                
                new_path = os.path.join(self.pages_dir, clean_name)
                
                # 处理文件夹合并与重命名
                if not os.path.exists(new_path):
                    os.rename(item_path, new_path)
                    target_dir = new_path
                else:
                    for f in os.listdir(item_path):
                        os.rename(os.path.join(item_path, f), os.path.join(new_path, f))
                    os.rmdir(item_path)
                    target_dir = new_path
                    
                # 批量修正文件内部的 frontmatter 的 folder 字段
                for f in os.listdir(target_dir):
                    if f.endswith('.md'):
                        f_path = os.path.join(target_dir, f)
                        meta = self._parse_page_file(f_path)
                        if meta and meta.get('folder') != clean_name:
                            md_content = f"---\nid: {meta['id']}\ntitle: \"{meta['title']}\"\nicon: \"{meta.get('icon','')}\"\nfolder: \"{clean_name}\"\ntags: \"{meta.get('tags','')}\"\n---\n{meta['html_content']}\n"
                            with open(f_path, 'w', encoding='utf-8') as file_obj:
                                file_obj.write(md_content)

    def _sync_pages(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pages") 
            
            unclassified_dir = os.path.join(self.pages_dir, '未分类')
            os.makedirs(unclassified_dir, exist_ok=True)
            for file in os.listdir(self.pages_dir):
                filepath = os.path.join(self.pages_dir, file)
                if os.path.isfile(filepath) and file.endswith('.md'):
                    os.rename(filepath, os.path.join(unclassified_dir, file))
            
            for root_dir, dirs, files in os.walk(self.pages_dir):
                if root_dir == self.pages_dir: continue
                folder_name = os.path.basename(root_dir)
                for file in files:
                    if not file.endswith('.md'): continue
                    filepath = os.path.join(root_dir, file)
                    meta = self._parse_page_file(filepath)
                    if meta and 'id' in meta:
                        cursor.execute('''
                            INSERT INTO pages (id, title, icon, folder, tags, html_content)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (
                            meta['id'], 
                            meta.get('title', '未命名项目'), 
                            meta.get('icon', ''), 
                            folder_name,
                            meta.get('tags', ''),
                            meta.get('html_content', '')
                        ))
            conn.commit()

    def get_all_pages(self) -> List[Dict]:
        with self._get_conn() as conn:
            return [dict(row) for row in conn.execute('SELECT id, title, icon, folder, tags FROM pages ORDER BY id DESC').fetchall()]

    def get_page_detail(self, page_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute('SELECT id, title, icon, folder, tags, html_content FROM pages WHERE id = ?', (page_id,)).fetchone()
            return dict(row) if row else None

    def get_order(self) -> Dict:
        if os.path.exists(ORDER_FILE):
            try:
                with open(ORDER_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: pass
        return {"group_order": [], "item_order": []}

engine = SandboxIndexEngine(pages_dir=PAGES_DIR, db_path=DB_FILE)

# ================= 数据 API 接口 =================

@sandbox_bp.route('/api/pages', methods=['GET'])
def get_pages():
    return jsonify({
        "pages": engine.get_all_pages(),
        "order": engine.get_order()
    })

@sandbox_bp.route('/api/pages', methods=['POST'])
def add_page():
    data = request.json
    title = data.get('title')
    icon = data.get('icon', '')
    
    # 强制清理：提取第一个分类作为 folder
    raw_folder = data.get('folder', '').strip()
    folder = re.split(r'[,，]', raw_folder)[0].strip() if raw_folder else '未分类'
    
    tags = data.get('tags', '')
    html_content = data.get('html_content')
    
    if not title or not html_content: return jsonify({"error": "标题和代码内容不能为空"}), 400
        
    try:
        page_id = f"p_{int(time.time())}"
        md_content = f"---\nid: {page_id}\ntitle: \"{title}\"\nicon: \"{icon}\"\nfolder: \"{folder}\"\ntags: \"{tags}\"\n---\n{html_content}\n"
        
        folder_path = os.path.join(PAGES_DIR, folder)
        os.makedirs(folder_path, exist_ok=True)
        
        with open(os.path.join(folder_path, f"{page_id}.md"), 'w', encoding='utf-8') as f:
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
    
    raw_folder = data.get('folder', '').strip()
    folder = re.split(r'[,，]', raw_folder)[0].strip() if raw_folder else '未分类'
    
    tags = data.get('tags', '')
    html_content = data.get('html_content')
    
    if not title or not html_content: return jsonify({"error": "标题和代码内容不能为空"}), 400

    old_path = engine._get_page_path(page_id)
    if not old_path: return jsonify({"error": "项目文件不存在"}), 404

    try:
        new_folder_path = os.path.join(PAGES_DIR, folder)
        os.makedirs(new_folder_path, exist_ok=True)
        new_path = os.path.join(new_folder_path, f"{page_id}.md")
        
        md_content = f"---\nid: {page_id}\ntitle: \"{title}\"\nicon: \"{icon}\"\nfolder: \"{folder}\"\ntags: \"{tags}\"\n---\n{html_content}\n"
        
        if old_path != new_path:
            os.remove(old_path)
            
        with open(new_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
            
        engine._sync_pages()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/api/pages/<page_id>/move', methods=['PUT'])
def move_page(page_id):
    """纯指令移动：修改物理路径，并更新文件头部元数据"""
    raw_new_folder = request.json.get('new_folder', '').strip()
    new_folder = re.split(r'[,，]', raw_new_folder)[0].strip() if raw_new_folder else '未分类'
    
    old_path = engine._get_page_path(page_id)
    if not old_path: return jsonify({"error": "文件不存在"}), 404
    
    try:
        meta = engine._parse_page_file(old_path)
        new_folder_path = os.path.join(PAGES_DIR, new_folder)
        os.makedirs(new_folder_path, exist_ok=True)
        new_path = os.path.join(new_folder_path, f"{page_id}.md")
        
        md_content = f"---\nid: {meta['id']}\ntitle: \"{meta['title']}\"\nicon: \"{meta.get('icon','')}\"\nfolder: \"{new_folder}\"\ntags: \"{meta.get('tags','')}\"\n---\n{meta['html_content']}\n"
        
        if old_path != new_path:
            os.remove(old_path)
            with open(new_path, 'w', encoding='utf-8') as f:
                f.write(md_content)
        
        engine._sync_pages()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/api/groups/rename', methods=['PUT'])
def rename_group():
    data = request.json
    old_name = data.get('old_name', '').strip()
    
    raw_new_name = data.get('new_name', '').strip()
    new_name = re.split(r'[,，]', raw_new_name)[0].strip() if raw_new_name else '未分类'
    
    if not old_name or old_name == new_name:
        return jsonify({"error": "无效的名称"}), 400
        
    old_dir = os.path.join(PAGES_DIR, old_name)
    new_dir = os.path.join(PAGES_DIR, new_name)
    
    if not os.path.exists(old_dir): return jsonify({"error": "原分组不存在"}), 404
        
    try:
        if os.path.exists(new_dir):
            for file in os.listdir(old_dir):
                os.rename(os.path.join(old_dir, file), os.path.join(new_dir, file))
            os.rmdir(old_dir)
        else:
            os.rename(old_dir, new_dir)
            
        for root, _, files in os.walk(new_dir):
            for file in files:
                if file.endswith('.md'):
                    path = os.path.join(root, file)
                    meta = engine._parse_page_file(path)
                    md_content = f"---\nid: {meta['id']}\ntitle: \"{meta['title']}\"\nicon: \"{meta.get('icon','')}\"\nfolder: \"{new_name}\"\ntags: \"{meta.get('tags','')}\"\n---\n{meta['html_content']}\n"
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(md_content)

        engine._sync_pages()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/api/order', methods=['PUT'])
def update_order():
    data = request.json
    try:
        with open(ORDER_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/api/pages/<page_id>', methods=['DELETE'])
def delete_page(page_id):
    md_path = engine._get_page_path(page_id)
    if not md_path: return jsonify({"error": "项目文件不存在"}), 404
    try:
        os.remove(md_path)
        engine._sync_pages()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sandbox_bp.route('/api/export_untagged', methods=['GET'])
def export_untagged():
    with engine._get_conn() as conn:
        rows = conn.execute("SELECT id, title, html_content FROM pages WHERE folder = '未分类' AND tags = ''").fetchall()
    
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['id', 'title', 'tags', 'content_snippet'])
    
    for r in rows:
        raw_html = r['html_content']
        clean_text = re.sub(r'<(script|style).*?>.*?</\1>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
        clean_text = re.sub(r'<[^>]+>', '', clean_text)
        clean_text = html.unescape(clean_text)
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
            raw_folder = row.get('folder', '').strip()
            
            # 严格提取单个词作为 folder
            if not raw_folder and new_tags:
                new_folder = re.split(r'[,，]', new_tags)[0].strip()
            else:
                new_folder = re.split(r'[,，]', raw_folder)[0].strip() if raw_folder else '未分类'
            
            if page_id and (new_tags or new_folder != '未分类'):
                old_path = engine._get_page_path(page_id)
                if old_path:
                    meta = engine._parse_page_file(old_path)
                    if meta:
                        target_folder = new_folder if new_folder != '未分类' else meta.get('folder', '未分类')
                        target_folder = re.split(r'[,，]', target_folder)[0].strip()
                        target_tags = new_tags if new_tags else meta.get('tags', '')
                        
                        new_folder_path = os.path.join(PAGES_DIR, target_folder)
                        os.makedirs(new_folder_path, exist_ok=True)
                        new_path = os.path.join(new_folder_path, f"{page_id}.md")
                        
                        md_content = f"---\nid: {meta['id']}\ntitle: \"{meta['title']}\"\nicon: \"{meta.get('icon','')}\"\nfolder: \"{target_folder}\"\ntags: \"{target_tags}\"\n---\n{meta['html_content']}\n"
                        
                        if old_path != new_path: os.remove(old_path)
                        with open(new_path, 'w', encoding='utf-8') as f:
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
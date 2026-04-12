import os
import sqlite3
import json
import time
import re
import traceback
from collections import Counter
from typing import List, Dict, Optional
from flask import Blueprint, render_template, request, jsonify, send_from_directory

# ================= 基础配置与目录初始化 =================
BASE_DIR = os.path.abspath("db/card")
CARDS_DIR = os.path.join(BASE_DIR, "cards_data")
COVERS_DIR = os.path.join(BASE_DIR, "covers")
DB_FILE = os.path.join(BASE_DIR, "data.db")

os.makedirs(CARDS_DIR, exist_ok=True)
os.makedirs(COVERS_DIR, exist_ok=True)

card_bp = Blueprint('card', __name__, url_prefix='/card-tag')

@card_bp.errorhandler(Exception)
def handle_exception(e):
    error_detail = traceback.format_exc()
    print("【认知层后端发生异常】:\n", error_detail)
    return jsonify({"status": "error", "message": f"服务器内部错误: {str(e)}"}), 500

# ================= 核心引擎层 =================
# ================= 核心引擎层 =================
class CardIndexEngine:
    def __init__(self, cards_dir: str, db_path: str):
        self.cards_dir = cards_dir
        self.db_path = db_path
        self._init_db()
        self._sync_cards()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            # 暴力重构表结构，确保新增的 stats_data 字段生效
            conn.execute('DROP TABLE IF EXISTS cards')
            conn.execute('''
                CREATE TABLE cards (
                    id TEXT PRIMARY KEY,
                    type TEXT,
                    query TEXT,
                    tags TEXT,
                    cover TEXT,
                    stats_data TEXT,
                    content_body TEXT
                )
            ''')
            conn.commit()

    def _parse_card_file(self, filepath: str) -> Optional[Dict]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                # 剔除头部 BOM 和首尾空白，增强容错
                content = f.read().lstrip('\ufeff').strip()
                
            if not content.startswith('---'):
                return None
                
            parts = content.split('---', 2)
            if len(parts) < 3:
                return None
            
            meta = {}
            for line in parts[1].strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                if ':' in line:
                    key, val = line.split(':', 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    
                    if key == 'tags' and val.startswith('[') and val.endswith(']'):
                        tag_str = val[1:-1]
                        meta[key] = [t.strip().strip('"').strip("'") for t in tag_str.split(',') if t.strip()]
                    else:
                        meta[key] = val
            meta['content_body'] = parts[2].strip()
            return meta
        except Exception as e:
            print(f"解析文件失败 {filepath}: {e}")
            return None

    def _sync_cards(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cards") 
            for filename in os.listdir(self.cards_dir):
                if not filename.endswith('.md'):
                    continue
                meta = self._parse_card_file(os.path.join(self.cards_dir, filename))
                if meta and 'id' in meta:
                    tags_json = json.dumps(meta.get('tags', []), ensure_ascii=False)
                    stats_data = meta.get('stats_data', '')
                    cursor.execute('''
                        INSERT INTO cards (id, type, query, tags, cover, stats_data, content_body)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        meta['id'], meta.get('type', 'unknown'), meta.get('query', ''),
                        tags_json, meta.get('cover', ''), stats_data,
                        meta.get('content_body', '')
                    ))
            conn.commit()

    def search(self, query_text: str = "", tags: List[str] = None) -> List[Dict]:
        sql = "SELECT id, type, query, tags, cover, stats_data, content_body FROM cards WHERE 1=1 ORDER BY id DESC"
        params = []
        if query_text:
            sql += " AND query LIKE ?"
            params.append(f"%{query_text}%")
        if tags:
            for tag in tags:
                sql += " AND tags LIKE ?"
                params.append(f'%"{tag}"%')
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            results = []
            for row in cursor.fetchall():
                res = dict(row)
                
                # 反序列化 tags
                try:
                    res['tags'] = json.loads(res['tags']) if res['tags'] else []
                except Exception:
                    res['tags'] = []
                    
                # 反序列化隐式图表数据
                try:
                    res['stats_data'] = json.loads(res['stats_data']) if res['stats_data'] else None
                except Exception:
                    res['stats_data'] = None
                    
                results.append(res)
            return results

    def get_all_tags(self) -> List[str]:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tags FROM cards")
            all_tags = set()
            for row in cursor.fetchall():
                try:
                    tags = json.loads(row['tags'])
                    if isinstance(tags, list):
                        all_tags.update(tags)
                except Exception:
                    continue
            return sorted(list(all_tags))

    def get_stats(self) -> Dict:
        """获取系统宏观统计"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT count(1) FROM cards")
            total_cards = cursor.fetchone()[0]

            cursor.execute("SELECT tags FROM cards")
            tag_set = set()
            for row in cursor.fetchall():
                try:
                    tags = json.loads(row['tags'])
                    if isinstance(tags, list):
                        tag_set.update(tags)
                except Exception:
                    continue
            return {
                "total_cards": total_cards,
                "total_tags": len(tag_set)
            }

    def get_tag_frequencies(self) -> List[Dict]:
        """获取带有频次分布的标签列表，用于健康度分析"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tags FROM cards")
            from collections import Counter
            counter = Counter()
            total_tag_instances = 0
            for row in cursor.fetchall():
                try:
                    tags = json.loads(row['tags'])
                    if isinstance(tags, list):
                        counter.update(tags)
                        total_tag_instances += len(tags)
                except Exception:
                    continue
            
            sorted_tags = [{"tag": k, "count": v} for k, v in counter.most_common()]
            return {
                "tag_frequencies": sorted_tags,
                "total_instances": total_tag_instances
            }

engine = CardIndexEngine(cards_dir=CARDS_DIR, db_path=DB_FILE)
engine = CardIndexEngine(cards_dir=CARDS_DIR, db_path=DB_FILE)

# ================= 蓝图路由层 =================

@card_bp.route('/')
def index():
    return render_template('index.html')
CardIndexEngine
@card_bp.route('/tags_dashboard')
def tags_dashboard():
    return render_template('tags_dashboard.html')

@card_bp.route('/api/tags')
def get_tags():
    return jsonify(engine.get_all_tags())

@card_bp.route('/api/stats')
def get_stats():
    return jsonify(engine.get_stats())

@card_bp.route('/api/tags_freq')
def get_tags_freq():
    return jsonify(engine.get_tag_frequencies())

@card_bp.route('/api/search')
@card_bp.route('/api/skip/search')
def search_cards():
    query = request.args.get('q', '')
    tags_param = request.args.get('tags', '')
    tags = [t for t in tags_param.split(',') if t] if tags_param else None
    results = engine.search(query_text=query, tags=tags)
    return jsonify(results)

@card_bp.route('/api/sync', methods=['POST'])
def sync_data():
    engine._sync_cards()
    return jsonify({"status": "success", "message": "索引已刷新"})

@card_bp.route('/covers/<path:filename>')
def serve_cover(filename):
    return send_from_directory(COVERS_DIR, filename)

@card_bp.route('/api/edit/<card_id>', methods=['POST'])
def edit_card(card_id):
    md_path = os.path.join(CARDS_DIR, f"{card_id}.md")
    if not os.path.exists(md_path):
        return jsonify({"status": "error", "message": "卡片文件不存在"}), 404

    tags_str = request.form.get('tags', '')
    tags_list = [t.strip() for t in re.split(r'[,\s;，；、]+', tags_str) if t.strip()]
    tags_fm = json.dumps(tags_list, ensure_ascii=False)

    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    parts = content.split('---', 2)
    if len(parts) >= 3:
        fm_lines = parts[1].strip().split('\n')
        new_fm_lines = []
        for line in fm_lines:
            if line.startswith('tags:'):
                new_fm_lines.append(f"tags: {tags_fm}")
            else:
                new_fm_lines.append(line)
        new_content = f"---\n{chr(10).join(new_fm_lines)}\n---{parts[2]}"
        
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        engine._sync_cards()
        return jsonify({"status": "success", "message": "标签更新成功"})
    else:
        return jsonify({"status": "error", "message": "Markdown Frontmatter 格式损坏"}), 500

@card_bp.route('/api/upload', methods=['POST'])
def upload_card():
    if 'cover' not in request.files:
        return jsonify({"status": "error", "message": "缺少封面图片"}), 400
        
    file = request.files['cover']
    if file.filename == '':
        return jsonify({"status": "error", "message": "未选择文件"}), 400

    query = request.form.get('query', '未命名卡片').strip()
    tags_str = request.form.get('tags', '')
    description = request.form.get('description', '').strip()
    
    card_id = f"c_{int(time.time())}"
    
    ext = os.path.splitext(file.filename)[1].lower()
    if not ext:
        ext = '.jpg'
    cover_filename = f"{card_id}{ext}"
    file.save(os.path.join(COVERS_DIR, cover_filename))
    
    tags_list = [t.strip() for t in re.split(r'[,\s;，；、]+', tags_str) if t.strip()]
    tags_fm = json.dumps(tags_list, ensure_ascii=False)
    
    md_content = f"""---
id: {card_id}
type: user_upload
query: "{query}"
tags: {tags_fm}
cover: "./covers/{cover_filename}"
---

{description}
"""
    md_path = os.path.join(CARDS_DIR, f"{card_id}.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md_content)
        
    engine._sync_cards()
    return jsonify({"status": "success", "message": "卡片录入成功"})

# ================= 接收客户端推送的统计数据并更新 MD 卡片 =================
# ================= 接收客户端推送的统计数据 (隐藏至 YAML 头部) =================
@card_bp.route('/api/skip/update_stats/<card_id>', methods=['POST'])
@card_bp.route('/api/update_stats/<card_id>', methods=['POST'])
# ================= 接收客户端推送的统计数据 (隐藏至 YAML 头部) =================
def update_card_stats(card_id):
    stats = request.json
    print(stats)
    if not stats:
        return jsonify({"status": "error", "message": "未收到统计数据"}), 400

    md_path = os.path.join(CARDS_DIR, f"{card_id}.md")
    if not os.path.exists(md_path):
        return jsonify({"status": "error", "message": "卡片文件不存在"}), 404

    try:
        # 强制将 Windows换行符 统一转换为 标准换行符，杜绝隐形 \r 导致的解析灾难
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read().replace('\r\n', '\n')

        parts = content.split('---', 2)
        if len(parts) < 3:
            return jsonify({"status": "error", "message": "Markdown Frontmatter 格式损坏"}), 500

        fm_text = parts[1]
        body_text = parts[2]

        # 1. 整理 Frontmatter
        fm_lines = fm_text.strip().split('\n')
        new_fm_lines = []
        for line in fm_lines:
            # 过滤空行和旧的 stats_data
            if line.strip() and not line.startswith('stats_data:'):
                new_fm_lines.append(line)
        
        # 压入新的隐式数据
        stats_json_str = json.dumps(stats, ensure_ascii=False)
        new_fm_lines.append(f"stats_data: '{stats_json_str}'")
        fm_str = '\n'.join(new_fm_lines)

        # 2. 安全清理正文中的 "# 数据统计" 旧区块（不使用正则）
        target_header = "# 数据统计\n"
        if target_header in body_text:
            start_idx = body_text.find(target_header)
            # 找寻下一个 # 标题，作为清除的终点
            next_header_idx = body_text.find("\n# ", start_idx + len(target_header))
            
            if next_header_idx != -1:
                # 拼接头部和下方的其他内容
                body_text = body_text[:start_idx] + body_text[next_header_idx:]
            else:
                # 下面没有其他标题了，直接截断
                body_text = body_text[:start_idx]

        # 整理正文换行，保持整洁
        body_text = body_text.strip('\n')
        body_text = f"\n\n{body_text}\n" if body_text else "\n"

        # 3. 重新组装并写入
        new_content = f"---\n{fm_str}\n---{body_text}"

        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        # 触发核心引擎刷新
        engine._sync_cards()
        return jsonify({"status": "success", "message": "图表数据已安全注入卡片头部"})
        
    except Exception as e:
        print(f"写入文件异常: {e}")
        return jsonify({"status": "error", "message": f"服务器写入异常: {str(e)}"}), 500
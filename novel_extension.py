# novel_extension.py
from flask import Blueprint, request, jsonify
import sqlite3
import os
import re
import ollama

novel_bp = Blueprint('novel_bp', __name__)
DB_PATH = 'universal_data.db'

def init_novel_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS novels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT,
                category TEXT,
                tags TEXT,
                intro TEXT,
                status TEXT DEFAULT '未知',
                cover TEXT,
                create_time DATETIME DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS novel_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER,
                chapter_index INTEGER,
                chapter_name TEXT,
                content TEXT,
                FOREIGN KEY(novel_id) REFERENCES novels(id)
            )
        ''')
init_novel_db()

# ================= 基础数据 API =================

@novel_bp.route('/api/novel/list', methods=['GET'])
def get_novels():
    category = request.args.get('category')
    search = request.args.get('search')
    query = 'SELECT id, title, author, category, cover, status FROM novels WHERE 1=1'
    params = []
    
    if category:
        query += ' AND category = ?'
        params.append(category)
    if search:
        query += ' AND (title LIKE ? OR author LIKE ?)'
        params.extend([f'%{search}%', f'%{search}%'])
        
    query += ' ORDER BY id DESC'
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
    result = [{"id": r[0], "title": r[1], "author": r[2], "category": r[3], "cover": r[4], "status": r[5]} for r in rows]
    return jsonify(result)

@novel_bp.route('/api/novel/detail/<int:novel_id>', methods=['GET'])
def get_novel_detail(novel_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT title, author, category, tags, intro, status FROM novels WHERE id=?', (novel_id,))
        novel = cursor.fetchone()
        if not novel:
            return jsonify({"error": "Novel not found"}), 404
            
        cursor.execute('SELECT id, chapter_name, chapter_index FROM novel_chapters WHERE novel_id=? ORDER BY chapter_index ASC', (novel_id,))
        chapters = [{"id": r[0], "name": r[1], "index": r[2]} for r in cursor.fetchall()]
        
    return jsonify({
        "info": {"title": novel[0], "author": novel[1], "category": novel[2], "tags": novel[3], "intro": novel[4], "status": novel[5]},
        "chapters": chapters
    })

@novel_bp.route('/api/novel/chapter/<int:chapter_id>', methods=['GET'])
def get_chapter(chapter_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT novel_id, chapter_name, content, chapter_index FROM novel_chapters WHERE id=?', (chapter_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Chapter not found"}), 404
            
        novel_id = row[0]
        # 获取上一章和下一章ID
        cursor.execute('SELECT id FROM novel_chapters WHERE novel_id=? AND chapter_index<? ORDER BY chapter_index DESC LIMIT 1', (novel_id, row[3]))
        prev_id = cursor.fetchone()
        cursor.execute('SELECT id FROM novel_chapters WHERE novel_id=? AND chapter_index>? ORDER BY chapter_index ASC LIMIT 1', (novel_id, row[3]))
        next_id = cursor.fetchone()

    return jsonify({
        "novel_id": novel_id,
        "chapter_name": row[1],
        "content": row[2],
        "prev_id": prev_id[0] if prev_id else None,
        "next_id": next_id[0] if next_id else None
    })

# ================= AI 处理核心 API =================

@novel_bp.route('/api/novel/ai_init', methods=['POST'])
def ai_init_novel():
    """接收本地 TXT 文件路径，进行全自动 AI 初始化"""
    data = request.json
    file_path = data.get('file_path')
    title = data.get('title', '未知书籍')
    model_name = data.get('model', 'qwen3:9b')

    if not os.path.exists(file_path):
        return jsonify({"error": "文件不存在"}), 400

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        full_text = f.read()

    text_length = len(full_text)
    head_text = full_text[:10000]
    tail_text = full_text[-1000:] if text_length > 10000 else ""

    # 1. AI 分析简介与状态
    intro_prompt = f"请阅读以下小说开局，提炼200字核心设定简介、流派标签(以逗号分隔)。\n正文：{head_text[:3000]}"
    status_prompt = f"阅读小说末尾，判断是'完本'、'连载'还是'断更'。\n正文：{tail_text}"
    
    try:
        intro_res = ollama.generate(model=model_name, prompt=intro_prompt)['response']
        status_res = ollama.generate(model=model_name, prompt=status_prompt)['response']
    except Exception as e:
        return jsonify({"error": f"Ollama 调用失败: {str(e)}"}), 500

    # 提取标签逻辑 (简单容错)
    tags = "未分类"
    if "标签:" in intro_res or "标签：" in intro_res:
        tags = intro_res.split("标签")[1].split("\n")[0].strip(":： ")

    # 2. 存入主表获取 ID
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO novels (title, intro, status, tags) VALUES (?, ?, ?, ?)', 
                       (title, intro_res, status_res, tags))
        novel_id = cursor.lastrowid

    # 3. 智能分章
    chapters = []
    # 规则 1：尝试标准正则
    pattern = re.compile(r'(第[零一二三四五六七八九十百千万\d]+[章卷节回] .*?)(?=\n第[零一二三四五六七八九十百千万\d]+[章卷节回] |\Z)', re.DOTALL)
    matches = pattern.findall(full_text)
    
    if len(matches) > 10: # 正则生效
        for idx, match in enumerate(matches):
            lines = match.strip().split('\n', 1)
            c_name = lines[0].strip()
            c_content = lines[1].strip() if len(lines) > 1 else ""
            chapters.append((novel_id, idx, c_name, c_content))
    else:
        # 规则 2：正则失败，寻找前1万字的潜在分章特征 (伪代码逻辑实现，实际可用 AI 返回特征字符)
        # 此处使用暴力按字数切分兜底，或者调用小模型（如果配置了本地小模型如 qwen:0.5b）
        chunk_size = 3000
        for i in range(0, text_length, chunk_size):
            chunk = full_text[i:i+chunk_size]
            c_name = f"第 {i//chunk_size + 1} 部分"
            chapters.append((novel_id, i//chunk_size, c_name, chunk))

    # 批量插入章节
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.executemany('INSERT INTO novel_chapters (novel_id, chapter_index, chapter_name, content) VALUES (?, ?, ?, ?)', chapters)

    return jsonify({"status": "success", "novel_id": novel_id, "intro": intro_res, "chapter_count": len(chapters)})
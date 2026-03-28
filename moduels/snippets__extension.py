import os
import sqlite3
from datetime import datetime
from flask import Blueprint, jsonify, request

# ====================================================
# [业务逻辑] Snippets Blueprint (基于 SQLite)
# ====================================================
snippets_bp = Blueprint('snippets', __name__)
DB_PATH = 'db/snippets.db'

def init_db():
    """初始化数据库表结构"""
    # 确保目录存在
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 创建代码片段表 (id主键保证改名安全, created_at用于排序)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS snippets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# 蓝图加载时自动初始化数据库
init_db()

def get_db_connection():
    """获取数据库连接，并设置字典格式返回"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@snippets_bp.route('/api/snippets/list', methods=['GET'])
def list_snippets():
    """获取列表：按首次添加时间倒序排序（最新的在最前）"""
    conn = get_db_connection()
    # 如果想最早添加的在最前，将 DESC 改为 ASC 即可
    snippets = conn.execute('SELECT id, title, created_at FROM snippets ORDER BY created_at DESC').fetchall()
    conn.close()
    
    return jsonify({"snippets": [dict(s) for s in snippets]})

@snippets_bp.route('/api/snippets/raw/<int:snippet_id>', methods=['GET'])
def get_snippet(snippet_id):
    """获取单条记录的源码，用于在前端编辑器回显"""
    conn = get_db_connection()
    snippet = conn.execute('SELECT id, title, content FROM snippets WHERE id = ?', (snippet_id,)).fetchone()
    conn.close()
    
    if snippet is None:
        return jsonify({"error": "记录不存在"}), 404
        
    return jsonify(dict(snippet))

@snippets_bp.route('/api/snippets/save', methods=['POST'])
def save_snippet():
    """新建或更新代码片段 (包含改名与修改内容)"""
    data = request.json
    snippet_id = data.get('id')  # 如果有 id 则为更新，无 id 则为新建
    title = data.get('title', '').strip()
    content = data.get('content', '')

    if not title:
        return jsonify({"error": "标题不能为空"}), 400

    conn = get_db_connection()
    try:
        if snippet_id:
            # 修改/改名模式
            conn.execute('UPDATE snippets SET title = ?, content = ? WHERE id = ?', 
                         (title, content, snippet_id))
        else:
            # 新建模式
            conn.execute('INSERT INTO snippets (title, content) VALUES (?, ?)', 
                         (title, content))
        conn.commit()
        return jsonify({"message": "保存成功"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@snippets_bp.route('/api/snippets/delete/<int:snippet_id>', methods=['DELETE'])
def delete_snippet(snippet_id):
    """删除指定的代码片段"""
    conn = get_db_connection()
    try:
        cursor = conn.execute('DELETE FROM snippets WHERE id = ?', (snippet_id,))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "记录不存在"}), 404
        return jsonify({"message": "删除成功"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
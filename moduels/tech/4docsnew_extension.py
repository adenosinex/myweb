import os
import re
import yaml
import json
import hashlib
import requests
import time
import shutil
from datetime import datetime
from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

document_bp = Blueprint('document', __name__)

DOCS_DIR = 'db/docsnew_data' 
INBOX_DIR = os.path.join(DOCS_DIR, 'inbox')
ARCHIVE_DIR = os.path.join(DOCS_DIR, 'archive')

# 自动分子文件夹配置：每个文件夹最大文件数
MAX_FILES_PER_FOLDER = 100

# 内存索引（替代 SQLite，避免高频磁盘 I/O）
FILE_INDEX = {}

for d in [DOCS_DIR, INBOX_DIR, ARCHIVE_DIR]:
    os.makedirs(d, exist_ok=True)

def get_target_folder(base_dir):
    """根据数量自动分配子文件夹，满载则创建递增的新文件夹 (例如 001, 002)"""
    subdirs = []
    for d in os.listdir(base_dir):
        if os.path.isdir(os.path.join(base_dir, d)) and d.isdigit():
            subdirs.append(int(d))
            
    if not subdirs:
        target = os.path.join(base_dir, "001")
        os.makedirs(target, exist_ok=True)
        return target
        
    current_max = max(subdirs)
    target = os.path.join(base_dir, f"{current_max:03d}")
    
    files_in_target = [f for f in os.listdir(target) if f.endswith('.md')]
    if len(files_in_target) >= MAX_FILES_PER_FOLDER:
        target = os.path.join(base_dir, f"{current_max + 1:03d}")
        os.makedirs(target, exist_ok=True)
        
    return target

def calculate_hash(content: str) -> str:
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def parse_frontmatter(content: str):
    if content.startswith('---\n'):
        parts = content.split('---\n', 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1])
                if not isinstance(meta, dict): meta = {}
                return meta, parts[2]
            except Exception:
                return {}, parts[2]
    return {}, content
    
def build_frontmatter(meta: dict, content: str) -> str:
    yaml_str = yaml.dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{yaml_str}---\n{content}"

def build_memory_index():
    """重建全量文件内存索引"""
    global FILE_INDEX
    new_index = {}
    
    for dirpath, _, filenames in os.walk(DOCS_DIR):
        for f in filenames:
            if not f.endswith('.md'): continue
            
            full_path = os.path.join(dirpath, f)
            rel_path = os.path.relpath(full_path, DOCS_DIR)
            
            try:
                with open(full_path, 'r', encoding='utf-8') as file:
                    raw_content = file.read()
                
                meta, pure_content = parse_frontmatter(raw_content)
                doc_id = meta.get('id', secure_filename(f[:-3]))
                status = 'archive' if 'archive' in rel_path.lower() else 'active'
                
                tags_list = meta.get('tags', [])
                if not isinstance(tags_list, list):
                    tags_list = [str(tags_list)]
                
                new_index[doc_id] = {
                    'id': doc_id,
                    'path': rel_path.replace('\\', '/'),
                    'title': meta.get('title', f[:-3]),
                    'status': status,
                    'is_pinned': 1 if meta.get('pinned') else 0,
                    'tags': tags_list,
                    'tags_cache': ",".join(tags_list),
                    'created_time': os.path.getctime(full_path),
                    'update_time': os.path.getmtime(full_path),
                    'file_size': os.path.getsize(full_path),
                    'word_count': len(pure_content.strip())
                }
            except Exception as e:
                print(f"解析文件失败 {full_path}: {e}")
                
    FILE_INDEX = new_index

# 启动时初始化索引
build_memory_index()

def generate_tags_from_ai(content: str) -> list:
    api_key = os.getenv('OP_API_KEY')
    if not api_key: return ["API_KEY_MISSING"]

    snippet = content[:800].strip()
    if not snippet: return []

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "You are a keyword extraction assistant. Extract 3 to 5 highly relevant keywords in Chinese. Return ONLY a comma-separated list of keywords, NO JSON, NO explanations."},
            {"role": "user", "content": f"Text:\n\n{snippet}"}
        ],
        "max_tokens": 100,
        "temperature": 0.2
    }

    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=20)
        response.raise_for_status()
        raw_text = response.json()['choices'][0]['message']['content'].strip()
        raw_text = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL).strip()
        
        raw_tags = []
        try:
            json_str = re.sub(r'^```json\s*', '', raw_text, flags=re.IGNORECASE)
            json_str = re.sub(r'\s*```$', '', json_str)
            parsed = json.loads(json_str)
            if isinstance(parsed, dict) and 'tags' in parsed: raw_tags = parsed['tags']
            elif isinstance(parsed, list): raw_tags = parsed
            else: raise ValueError("Not a valid tag structure")
        except:
            raw_tags = re.split(r'[,，、\n;；]+', raw_text.replace('"', '').replace('`', '').replace('[', '').replace(']', '').replace('{', '').replace('}', ''))

        clean_tags = []
        for t in raw_tags:
            t_clean = re.sub(r'[^\w\u4e00-\u9fa5\-]', '', str(t)).strip()
            if 1 < len(t_clean) <= 15 and t_clean.lower() not in ['tags', 'keywords', '关键词', '标签']:
                clean_tags.append(t_clean)
        return clean_tags[:5]
    except Exception as e:
        print(f"OpenRouter API Error: {e}")
        return ["AI_ERROR"]
        
@document_bp.route('/docapi/init', methods=['POST'])
def init_system():
    build_memory_index()
    return jsonify({"message": "扫描重建索引完成", "total": len(FILE_INDEX)})

@document_bp.route('/docapi/documents', methods=['GET'])
def list_documents():
    docs = list(FILE_INDEX.values())
    # 排序：优先按置顶，其次按更新时间倒序
    docs.sort(key=lambda x: (x['is_pinned'], x['update_time']), reverse=True)
    return jsonify({"docs": docs})

@document_bp.route('/docapi/document/<doc_id>', methods=['GET'])
def get_document(doc_id):
    doc_info = FILE_INDEX.get(doc_id)
    if not doc_info: return jsonify({"error": "文档不在索引中"}), 404
        
    full_path = os.path.join(DOCS_DIR, doc_info['path'])
    if not os.path.exists(full_path): return jsonify({"error": "物理文件丢失"}), 404
        
    with open(full_path, 'r', encoding='utf-8') as f:
        return jsonify({"id": doc_id, "content": f.read(), "meta": doc_info})

@document_bp.route('/docapi/document/save', methods=['POST'])
def save_document():
    data = request.json
    doc_id = data.get('id')
    raw_content = data.get('content', '')
    
    existing_doc = FILE_INDEX.get(doc_id)
    status = existing_doc['status'] if existing_doc else 'active'
    
    # 确定物理路径
    if existing_doc and os.path.exists(os.path.join(DOCS_DIR, existing_doc['path'])):
        rel_path = existing_doc['path']
    else:
        # 新文档，使用自动分子文件夹逻辑
        target_dir = get_target_folder(ARCHIVE_DIR) if status == 'archive' else get_target_folder(INBOX_DIR)
        rel_path = os.path.relpath(os.path.join(target_dir, f"{doc_id}.md"), DOCS_DIR)
        
    full_path = os.path.join(DOCS_DIR, rel_path)
    
    meta, pure_content = parse_frontmatter(raw_content)
    meta['updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta['status'] = status
    if 'id' not in meta: meta['id'] = doc_id
    
    final_content = build_frontmatter(meta, pure_content)
    
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(final_content)
        
    # 同步更新内存索引
    tags_list = meta.get('tags', [])
    FILE_INDEX[doc_id] = {
        'id': doc_id,
        'path': rel_path.replace('\\', '/'),
        'title': meta.get('title', doc_id),
        'status': status,
        'is_pinned': 1 if meta.get('pinned') else 0,
        'tags': tags_list,
        'tags_cache': ",".join(tags_list),
        'created_time': os.path.getctime(full_path),
        'update_time': os.path.getmtime(full_path),
        'file_size': os.path.getsize(full_path),
        'word_count': len(pure_content.strip())
    }
    
    return jsonify({"message": "保存成功"})

@document_bp.route('/docapi/document/action', methods=['POST'])
def document_action():
    data = request.json
    doc_id = data.get('id')
    action = data.get('action')
    
    doc_info = FILE_INDEX.get(doc_id)
    if not doc_info: return jsonify({"error": "文档不存在"}), 404
    
    full_path = os.path.join(DOCS_DIR, doc_info['path'])
    if not os.path.exists(full_path): return jsonify({"error": "物理文件丢失"}), 404
    
    with open(full_path, 'r', encoding='utf-8') as f:
        raw_content = f.read()
        
    meta, pure_content = parse_frontmatter(raw_content)
    new_status = doc_info['status']
    rel_path = doc_info['path']
    
    if action == 'toggle_pin':
        meta['pinned'] = not meta.get('pinned', False)
    elif action == 'toggle_archive':
        new_status = 'active' if doc_info['status'] == 'archive' else 'archive'
        meta['status'] = new_status
        # 归档/激活状态切换时，触发自动分文件夹逻辑进行移动
        target_dir = get_target_folder(ARCHIVE_DIR) if new_status == 'archive' else get_target_folder(INBOX_DIR)
        rel_path = os.path.relpath(os.path.join(target_dir, f"{doc_id}.md"), DOCS_DIR)
        
        new_full_path = os.path.join(DOCS_DIR, rel_path)
        shutil.move(full_path, new_full_path)
        full_path = new_full_path

    final_content = build_frontmatter(meta, pure_content)
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(final_content)
        
    # 更新内存索引
    FILE_INDEX[doc_id]['status'] = new_status
    FILE_INDEX[doc_id]['is_pinned'] = 1 if meta.get('pinned') else 0
    FILE_INDEX[doc_id]['path'] = rel_path.replace('\\', '/')
    FILE_INDEX[doc_id]['update_time'] = os.path.getmtime(full_path)
    
    return jsonify({"message": "操作成功"})

@document_bp.route('/docapi/document/ai/tags', methods=['POST'])
def get_ai_tags():
    data = request.json
    _, pure_content = parse_frontmatter(data.get('content', ''))
    tags = generate_tags_from_ai(pure_content)
    return jsonify({"tags": tags})

@document_bp.route('/docapi/ai/batch_tags', methods=['POST'])
def batch_ai_tags():
    # 查找无标签的文件
    pending_docs = [d for d in FILE_INDEX.values() if not d['tags'] or d['tags'] == ['未分类'] or d['tags'] == ['笔记']]
    if not pending_docs: return jsonify({"message": "无待处理文档", "processed": 0})
    
    processed = 0
    for doc in pending_docs[:2]:  # 每次限 2 篇
        full_path = os.path.join(DOCS_DIR, doc['path'])
        if not os.path.exists(full_path): continue
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        meta, pure_content = parse_frontmatter(content)
        tags = generate_tags_from_ai(pure_content)
        
        if tags and "ERROR" not in tags[0] and "MISSING" not in tags[0]:
            meta['tags'] = tags
            meta['updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_content = build_frontmatter(meta, pure_content)
            
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            FILE_INDEX[doc['id']]['tags'] = tags
            FILE_INDEX[doc['id']]['tags_cache'] = ",".join(tags)
            FILE_INDEX[doc['id']]['update_time'] = os.path.getmtime(full_path)
            processed += 1
            
    return jsonify({"message": "批量处理完成", "processed": processed})

@document_bp.route('/docapi/document/<doc_id>/related', methods=['GET'])
def get_related(doc_id):
    current_doc = FILE_INDEX.get(doc_id)
    if not current_doc or not current_doc.get('tags'): return jsonify([])
    
    target_tags = set([t for t in current_doc['tags'] if t not in ['未分类', '笔记']])
    if not target_tags: return jsonify([])
    
    results = []
    for other_id, other_doc in FILE_INDEX.items():
        if other_id == doc_id: continue
        overlap = len(target_tags & set(other_doc.get('tags', [])))
        if overlap > 0:
            results.append({"id": other_id, "title": other_doc['title'], "overlap": overlap})
            
    results.sort(key=lambda x: x['overlap'], reverse=True)
    return jsonify(results[:5])

@document_bp.route('/docapi/timeline', methods=['GET'])
def get_timeline():
    date_counts = {}
    for doc in FILE_INDEX.values():
        if doc.get('created_time'):
            dt = datetime.fromtimestamp(doc['created_time']).strftime('%Y-%m-%d')
            date_counts[dt] = date_counts.get(dt, 0) + 1
    return jsonify([[k, v] for k, v in date_counts.items()])

@document_bp.route('/docapi/search', methods=['GET'])
def search_documents():
    q = request.args.get('q', '').strip()
    if not q: return jsonify({"results": []})
        
    results = []
    q_lower = q.lower()
    
    for doc_id, doc in FILE_INDEX.items():
        # Tag 专属搜索
        if q.startswith('tag:'):
            tag_target = q_lower.replace('tag:', '').strip()
            if tag_target in doc['tags_cache'].lower():
                results.append(doc)
            continue
            
        # 匹配元数据
        if q_lower in doc['title'].lower() or q_lower in doc['tags_cache'].lower():
            results.append(doc)
            continue
            
        # 物理读取正文暴搜
        full_path = os.path.join(DOCS_DIR, doc['path'])
        if os.path.exists(full_path):
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    if q_lower in f.read().lower():
                        results.append(doc)
            except:
                pass
                
    return jsonify({"results": results})
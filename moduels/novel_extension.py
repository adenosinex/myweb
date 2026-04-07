import os
import io
import re
import csv
import time
import json
import sqlite3
import threading
from urllib.parse import quote
from collections import Counter

import requests
from flask import Blueprint, request, jsonify, make_response


# ==============================================================================
# [1] 基础配置区 (Configurations)
# ==============================================================================
novel_ai_bp = Blueprint('novel_ai', __name__, url_prefix='/api/novel')

# 外部节点与数据库配置
RESOURCE_NODE_URL = "http://one4.zin6.dpdns.org:8100"
VECTOR_NODE_URL = "http://15x4.zin6.dpdns.org:5001"
DB_PATH = 'db/universal_data.db'

# AI 模型配置
AI_MODEL = 'huihui_ai/qwen2.5-abliterate:3b-instruct'
AI_BASE_URL = "http://apple4.zin6.dpdns.org:11434/v1"


# ==============================================================================
# [2] 全局状态与缓存 (Global State & Cache)
# ==============================================================================
scan_state = {
    "is_running": False, 
    "total": 0, 
    "processed": 0, 
    "success_count": 0,
    "total_time_sec": 0, 
    "status_msg": "就绪", 
    "recent_results": [], 
    "current_task": None, 
    "ai_model": AI_MODEL.split('/')[-1]
}

stop_event = threading.Event()
scan_thread = None
start_time = 0

# 文本分章内存缓存 (Novel_Name -> Chapters)
text_cache = {}


# ==============================================================================
# [3] 数据库辅助函数 (Database Utilities)
# ==============================================================================
def save_to_db(collection, payload):
    """通用的 JSON 存储函数"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO store (collection, payload) VALUES (?, ?)', 
            (collection, json.dumps(payload, ensure_ascii=False))
        )

def get_analyzed_novels():
    """获取所有已进行 AI 分析的小说名称集合"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis'")
            return {json.loads(row[0]).get('novel_name') for row in cursor.fetchall()}
    except Exception:
        return set()


# ==============================================================================
# [4] 文本处理工具箱 (Text Processing Utilities)
# ==============================================================================
def get_and_split_chapters(novel_name):
    """拉取小说内容并通过正则拆分为独立章节"""
    if novel_name in text_cache: 
        return text_cache[novel_name]
        
    res = requests.get(f"{RESOURCE_NODE_URL}/api/novel/content/{novel_name}", timeout=10)
    if res.status_code != 200: 
        return []
        
    text = res.json().get('content', '')
    
    # 兼容性极强的章节匹配正则
    pattern = r'(?:^|\n)[ \t\u3000]*([（【《\[]?(?:正文[ \t\u3000]*)?第[ \t\u3000]*[零一二两三四五六七八九十百千万\d]+[ \t\u3000]*[章回节卷部集折篇][^\n]{0,50})'
    regex = re.compile(pattern)
    
    chapters, last_idx, last_title = [], 0, "前言"
    
    for match in regex.finditer(text):
        content = text[last_idx:match.start()].strip()
        # 过滤开头过短的“目录列表”等杂乱信息
        if len(content) > 20 or not chapters: 
            chapters.append({"title": last_title, "content": content})
            
        last_title = match.group(1).strip()
        last_idx = match.end()
        
    # 补充最后一章
    chapters.append({"title": last_title, "content": text[last_idx:].strip()})
    
    # 兜底机制：无明确章节时，按 5000 字强制切块
    if len(chapters) <= 1: 
        chapters = [{"title": f"第 {i//5000 + 1} 节", "content": text[i:i+5000]} for i in range(0, len(text), 5000)]
        
    text_cache[novel_name] = chapters
    return chapters


# ==============================================================================
# [5] AI 核心引擎 (AI Inference Core)
# ==============================================================================
def analyze_core(novel_name, is_batch=False):
    """核心大模型推理逻辑（提取首尾内容进行状态推断）"""
    content_res = requests.get(f"{RESOURCE_NODE_URL}/api/novel/content/{novel_name}", timeout=15)
    if content_res.status_code != 200:
        raise Exception("资源节点读取失败")
        
    full_text = content_res.json().get('content', '')
    word_count = len(full_text)
    if word_count == 0:
        raise Exception("文本内容为空")

    head_text = full_text[:7000]
    tail_text = full_text[-1500:] if word_count > 7000 else ""

    if is_batch:
        preview = head_text[:300] + "\n\n... [数据扫描中] ...\n\n" + (tail_text[-200:] if tail_text else "")
        scan_state["current_task"] = {"novel": novel_name, "preview": preview}

    system_prompt = "你是一个无情的文本提取机器。严格遵守格式，严禁分点，严禁寒暄。"
    user_prompt = f"""
任务：提取小说《{novel_name}》的情报。

【强制输出模板】（只能输出这三行，必须保留粗体标识，严禁输出任何其他废话！）
**内容简介**：[在这里写100字以内的一段话概括]
**完结状态**：[已完结/连载中]
**原因解释**：[一句话原因]

==== 分析目标：开头文本 ====
{head_text}

==== 分析目标：结尾文本 ====
{tail_text}
"""
    
    ollama_url = AI_BASE_URL.replace('/v1', '/api/chat')
    payload = {
        "model": AI_MODEL,
        "messages": [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt}
        ],
        "stream": False,
        "think": False, 
        "options": {
            "temperature": 0.1,  
            "num_predict": 500,
            "num_ctx": 12000
        }
    }
    
    start_ai_time = time.time()
    try:
        resp = requests.post(ollama_url, json=payload, timeout=120)
        resp.raise_for_status()
        raw_json = resp.json()
        result_text = raw_json.get("message", {}).get("content", "").strip()
    except Exception as e:
        raise Exception(f"模型请求失败: {str(e)}")

    elapsed_time = round(time.time() - start_ai_time, 1)
    
    if not result_text:
        result_text = "⚠️ 模型返回了空结果，请查看下方的原始响应数据。"

    model_short_name = AI_MODEL.split('/')[-1]
    final_output = f"📊 **总字数**：约 {word_count:,} 字\n⏱️ **AI 耗时**：{elapsed_time}s | 🧠 **模型**：{model_short_name}\n\n{result_text}"
    
    db_payload = {
        "novel_name": novel_name,
        "word_count": word_count,
        "analysis_result": final_output,
        "raw_prompt": user_prompt,
        "raw_response": json.dumps(raw_json, ensure_ascii=False, indent=2)
    }
    save_to_db("novel_analysis", db_payload)
    
    return db_payload

def _run_batch_scan():
    """后台批量扫描工作流"""
    global scan_state, start_time
    scan_state.update({
        "is_running": True, 
        "status_msg": "正在获取列表...", 
        "processed": 0, 
        "success_count": 0, 
        "recent_results": [], 
        "current_task": None
    })
    start_time = time.time()
    
    try:
        res = requests.get(f"{RESOURCE_NODE_URL}/api/novels/json", timeout=10)
        if res.status_code != 200: 
            raise Exception("连接资源节点失败")
            
        all_novels = res.json()
        analyzed_set = get_analyzed_novels()
        pending_novels = [n for n in all_novels if n not in analyzed_set]
        scan_state["total"] = len(pending_novels)
        
        if not pending_novels:
            scan_state["status_msg"] = "已全部分析完毕"
            return

        for novel in pending_novels:
            if stop_event.is_set():
                scan_state["status_msg"] = "任务中止"
                break
                
            scan_state["status_msg"] = f"正在分析: {novel}"
            scan_state["total_time_sec"] = int(time.time() - start_time)
            
            try:
                result = analyze_core(novel, is_batch=True)
                scan_state["success_count"] += 1
                stream_item = {
                    "filename": novel, 
                    "category": f"{result['word_count'] // 10000}万字", 
                    "ai_tags": ["已完结" if "已完结" in result['tags'] else "连载中"] # tags needs parsing from output, handled here implicitly if existing logic did
                }
                scan_state["recent_results"].insert(0, stream_item)
                if len(scan_state["recent_results"]) > 5: 
                    scan_state["recent_results"].pop()
            except Exception as e: 
                print(f"分析失败 {novel}: {e}")
                
            scan_state["processed"] += 1
            
        if not stop_event.is_set(): 
            scan_state["status_msg"] = "批量分析完成"
            
    except Exception as e: 
        scan_state["status_msg"] = f"异常停止: {e}"
    finally:
        scan_state.update({"is_running": False, "current_task": None, "total_time_sec": int(time.time() - start_time)})


# ==============================================================================
# [6] 基础业务 API (Frontend Display APIs)
# ==============================================================================
@novel_ai_bp.route('/list', methods=['GET'])
def list_novels_paged():
    """分页列表，支持搜索、标签联动，自动过滤回收站，并携带喜欢状态"""
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 24))
        search_kw = request.args.get('search', '').lower()
        selected_tags = [t for t in request.args.get('tags', '').split(',') if t.strip()]
        
        res = requests.get(f"{RESOURCE_NODE_URL}/api/novels/json", timeout=5)
        all_files = res.json() if res.status_code == 200 else []
        
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT payload FROM store WHERE collection='novel_state_deleted'")
            deleted_set = {json.loads(row[0])['filename'] for row in cursor.fetchall()}
            
            cursor.execute("SELECT payload FROM store WHERE collection='novel_state_fav'")
            fav_set = {json.loads(row[0])['filename'] for row in cursor.fetchall()}
            
            cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis'")
            analysis_map = {json.loads(r[0])['novel_name']: json.loads(r[0]) for r in cursor.fetchall()}

        filtered_files = []
        for fname in all_files:
            if fname in deleted_set: 
                continue
            
            display_name = fname.replace('.txt', '').lower()
            item_tags = analysis_map.get(fname, {}).get('tags', [])
            
            if search_kw and not (search_kw in display_name or any(search_kw in t.lower() for t in item_tags)): 
                continue
            if selected_tags and not all(t.lower() in display_name or t in item_tags for t in selected_tags): 
                continue
                
            filtered_files.append(fname)

        total = len(filtered_files)
        paged_files = filtered_files[(page-1)*size : page*size]
        
        items = []
        for f in paged_files:
            items.append({
                "filename": f, 
                "displayTitle": f.replace('.txt',''), 
                "tags": analysis_map.get(f, {}).get('tags', ['未解析']), 
                "wordCount": analysis_map.get(f, {}).get('word_count', 0),
                "isFav": f in fav_set
            })

        return jsonify({"items": items, "total": total, "has_more": (page*size) < total})
    except Exception as e: 
        return jsonify({"items": [], "error": str(e)})


@novel_ai_bp.route('/tags/stats', methods=['GET', 'POST'])
def get_tag_stats():
    """精准统计：合并文件名与AI标签，并排除回收站文件"""
    try:
        target_tags = request.json.get('tags', []) if request.method == 'POST' else []
        try:
            res = requests.get(f"{RESOURCE_NODE_URL}/api/novels/json", timeout=5)
            all_files = res.json() if res.status_code == 200 else []
        except: 
            all_files = []
        
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis'")
            file_tags_map = {json.loads(row[0])['novel_name']: json.loads(row[0]).get('tags', []) for row in cursor.fetchall()}
            
            cursor.execute("SELECT payload FROM store WHERE collection='novel_state_deleted'")
            deleted_set = {json.loads(row[0])['filename'] for row in cursor.fetchall()}

        if not target_tags and request.method == 'GET':
            full_set = set()
            for t in file_tags_map.values(): 
                full_set.update(t)
            target_tags = list(full_set)

        stats = {}
        for tag in target_tags:
            if not tag: 
                continue
            matched_files = set()
            tag_lower = tag.lower()
            
            for fname in all_files:
                if fname in deleted_set: 
                    continue
                if tag_lower in fname.lower() or tag in file_tags_map.get(fname, []):
                    matched_files.add(fname)
            stats[tag] = len(matched_files)
            
        return jsonify(stats)
    except Exception as e: 
        return jsonify({"error": str(e)}), 500


@novel_ai_bp.route('/state/<path:filename>', methods=['GET', 'POST'])
def handle_novel_state(filename):
    """查询或更新单本小说的收藏/隐藏状态"""
    try:
        clean_filename = filename.strip()
        target_payload = json.dumps({"filename": clean_filename}, ensure_ascii=False)

        if request.method == 'POST':
            data = request.json
            s_type = data.get('type')
            is_active = data.get('active', False)
            collection = f"novel_state_{s_type}"
            
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "DELETE FROM store WHERE collection=? AND payload LIKE ?", 
                    (collection, f'%"{clean_filename}"%')
                )
                if is_active:
                    conn.execute(
                        "INSERT INTO store (collection, payload) VALUES (?, ?)", 
                        (collection, target_payload)
                    )
            return jsonify({"status": "success", "active": is_active})
        
        else:
            states = {"fav": False, "deleted": False}
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                for s in ["fav", "deleted"]:
                    cursor.execute(
                        "SELECT id FROM store WHERE collection=? AND payload LIKE ?", 
                        (f"novel_state_{s}", f'%"{clean_filename}"%')
                    )
                    if cursor.fetchone():
                        states[s] = True
            return jsonify(states)
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@novel_ai_bp.route('/analysis/detail/<path:filename>', methods=['GET'])
@novel_ai_bp.route('/skip/analysis/detail/<path:filename>', methods=['GET'])
def get_novel_analysis_detail(filename):
    """精准查询单本书的 AI 分析数据"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query_param = f'%"{filename}"%'
            cursor.execute("""
                SELECT payload FROM store 
                WHERE collection='novel_analysis' 
                AND payload LIKE ? 
                ORDER BY id DESC LIMIT 1
            """, (query_param,))
            
            row = cursor.fetchone()
            if row:
                return jsonify(json.loads(row[0]))
            
            return jsonify({
                "novel_name": filename,
                "analysis_result": "⚠️ 该书籍尚未进行 AI 深度解析。请前往管理端启动扫描。",
                "tags": [],
                "word_count": 0,
                "not_found": True
            }), 200 
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==============================================================================
# [7] 阅读器与下载资源 API (Reader & Download APIs)
# ==============================================================================
@novel_ai_bp.route('/toc/<path:filename>', methods=['GET'])
def web_toc(filename):
    chapters = get_and_split_chapters(filename)
    return jsonify({
        "toc": [{"title": c["title"], "index": i} for i, c in enumerate(chapters)], 
        "total": len(chapters)
    })

@novel_ai_bp.route('/chapter/<path:filename>/<int:index>', methods=['GET'])
def web_chapter(filename, index):
    chapters = get_and_split_chapters(filename)
    if 0 <= index < len(chapters):
        return jsonify({"title": chapters[index]["title"], "content": chapters[index]["content"]})
    return jsonify({"error": "越界"}), 404

@novel_ai_bp.route('/download/<path:filename>', methods=['GET'])
def download_novel(filename):
    res = requests.get(f"{RESOURCE_NODE_URL}/api/novel/content/{filename}", timeout=10)
    response = make_response('\ufeff' + res.json().get('content', ''))
    encoded_name = quote(filename.split('/')[-1])
    response.headers["Content-Disposition"] = f"attachment; filename={encoded_name}; filename*=UTF-8''{encoded_name}"
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    return response


# ==============================================================================
# [8] Legado 兼容 API (Legado Integrations)
# ==============================================================================
@novel_ai_bp.route('/legado/search', methods=['GET'])
def legado_search():
    keyword = request.args.get('key', '').lower()
    try:
        all_novels = requests.get(f"{RESOURCE_NODE_URL}/api/novels/json", timeout=5).json()
        with sqlite3.connect(DB_PATH) as conn:
            analysis_dict = {
                json.loads(r[0])['novel_name']: json.loads(r[0]) 
                for r in conn.execute("SELECT payload FROM store WHERE collection='novel_analysis'").fetchall()
            }
            
        results = []
        for n in all_novels:
            if keyword in n.lower() or not keyword:
                analysis = analysis_dict.get(n, {})
                results.append({
                    "name": n.rsplit('.', 1)[0], 
                    "author": "云端", 
                    "filename": n, 
                    "intro": analysis.get('analysis_result', '').replace('**', ''), 
                    "kind": ",".join(analysis.get('tags', []))
                })
        return jsonify(results)
    except: 
        return jsonify([])

@novel_ai_bp.route('/legado/toc', methods=['GET'])
def legado_toc():
    filename = request.args.get('file')
    chapters = get_and_split_chapters(filename)
    return jsonify([{"name": c["title"], "index": i, "file": filename} for i, c in enumerate(chapters)])

@novel_ai_bp.route('/legado/chapter', methods=['GET'])
def legado_chapter():
    chapters = get_and_split_chapters(request.args.get('file'))
    idx = int(request.args.get('index', 0))
    return jsonify({"content": chapters[idx]["content"] if 0 <= idx < len(chapters) else "越界"})

@novel_ai_bp.route('/legado/source', methods=['GET'])
def get_legado_source():
    host = request.host_url.rstrip('/')
    code = request.args.get('code', '8888')
    source_json = [{
        "bookSourceGroup": "自建云端", 
        "bookSourceName": "云端幻境智能书库(直连版)", 
        "bookSourceType": "0", 
        "bookSourceUrl": host, 
        "enable": True, 
        "ruleBookAuthor": "$.author", 
        "ruleBookContent": "$.content", 
        "ruleBookIntro": "$.intro", 
        "ruleBookKind": "$.kind", 
        "ruleBookName": "$.name", 
        "ruleChapterList": "$[*]", 
        "ruleChapterName": "$.name", 
        "ruleContentUrl": f"/api/novel/legado/chapter?file={{{{$.file}}}}&index={{{{$.index}}}}&code={code}", 
        "ruleFindUrl": f"发现::/api/novel/legado/search?code={code}", 
        "ruleSearchUrl": f"/api/novel/legado/search?key={{{{key}}}}&code={code}", 
        "weight": 9999
    }]
    return jsonify(source_json)


# ==============================================================================
# [9] 系统与维护控制 API (System Maintenance & Control)
# ==============================================================================
@novel_ai_bp.route('/scan', methods=['POST'])
def control_scan():
    global scan_thread, stop_event
    action = request.json.get('action')
    if action == 'start' and not scan_state["is_running"]:
        stop_event.clear()
        scan_thread = threading.Thread(target=_run_batch_scan, daemon=True)
        scan_thread.start()
        return jsonify({"status": "success", "message": "扫描启动"})
    elif action == 'stop' and scan_state["is_running"]:
        stop_event.set()
        return jsonify({"status": "success", "message": "中止中..."})
    return jsonify({"error": "无效指令或状态"}), 400

@novel_ai_bp.route('/scan/status', methods=['GET'])
def scan_status():
    if scan_state["is_running"]: 
        scan_state["total_time_sec"] = int(time.time() - start_time)
    return jsonify(scan_state)

@novel_ai_bp.route('/reset', methods=['POST'])
def reset_database():
    target = request.json.get('novel_name')
    with sqlite3.connect(DB_PATH) as conn:
        if target: 
            conn.execute("DELETE FROM store WHERE collection='novel_analysis' AND payload LIKE ?", (f'%"{target}"%',))
        else: 
            conn.execute("DELETE FROM store WHERE collection='novel_analysis'")
    return jsonify({"status": "success"})

@novel_ai_bp.route('/skip/maintenance/list', methods=['GET'])
def get_maintenance_list():
    """获取所有被标记为 '喜欢' 和 '隐藏/删除' 的小说列表"""
    try:
        liked_files, deleted_files = [], []
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT payload FROM store WHERE collection='novel_state_fav'")
            for row in cursor.fetchall():
                liked_files.append(json.loads(row[0]).get('filename'))
                
            cursor.execute("SELECT payload FROM store WHERE collection='novel_state_deleted'")
            for row in cursor.fetchall():
                deleted_files.append(json.loads(row[0]).get('filename'))
                
        return jsonify({"liked": liked_files, "deleted": deleted_files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@novel_ai_bp.route('/skip/maintenance/confirm_delete', methods=['POST'])
def confirm_maintenance_delete():
    """接收维护脚本的确认，彻底清理数据库中有关这些书籍的所有数据"""
    try:
        filenames = request.json.get('filenames', [])
        if not filenames:
            return jsonify({"status": "success", "cleaned_count": 0})
            
        cleaned_count = 0
        with sqlite3.connect(DB_PATH) as conn:
            for fname in filenames:
                clean_fname = fname.strip()
                match_pattern = f'%"filename": "{clean_fname}"%'
                novel_name_pattern = f'%"novel_name": "{clean_fname}"%'
                
                conn.execute("DELETE FROM store WHERE collection='novel_state_fav' AND payload LIKE ?", (match_pattern,))
                conn.execute("DELETE FROM store WHERE collection='novel_state_deleted' AND payload LIKE ?", (match_pattern,))
                conn.execute("DELETE FROM store WHERE collection='novel_analysis' AND payload LIKE ?", (novel_name_pattern,))
                
                # 删除阅读进度
                import base64
                prog_key = f"progress_{base64.b64encode(clean_fname.encode('utf-8')).decode('utf-8')[:16]}"
                conn.execute("DELETE FROM store WHERE collection='kv' AND key=?", (prog_key,))
                
                cleaned_count += 1
                
        return jsonify({"status": "success", "cleaned_count": cleaned_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@novel_ai_bp.route('/export/csv', methods=['GET'])
def export_all_csv():
    """无视分页，全量导出所有 AI 解析结果为 CSV"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis'")
            rows = cursor.fetchall()

        si = io.StringIO()
        si.write('\ufeff')
        cw = csv.writer(si)
        cw.writerow(['小说文件名', '总字数', 'AI分析结果'])

        for row in rows:
            data = json.loads(row[0])
            cw.writerow([
                data.get('novel_name', ''), 
                data.get('word_count', 0), 
                data.get('analysis_result', '')
            ])

        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=Novel_AI_Full_Export.csv"
        output.headers["Content-type"] = "text/csv; charset=utf-8"
        return output

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==============================================================================
# [10] RAG 与向量图谱集成 (Vector Graph Integrations)
# ==============================================================================
@novel_ai_bp.route('/recommend/<path:filename>', methods=['GET'])
def get_recommendations(filename):
    """请求独立向量节点，获取与当前小说最相似的书籍"""
    try:
        node_url = f"{VECTOR_NODE_URL}/api/vector/similar?name={quote(filename)}&k=5"
        res = requests.get(node_url, timeout=5)
        
        if res.status_code == 200:
            return jsonify(res.json())
        elif res.status_code == 404:
            return jsonify({"recommendations": [], "msg": "向量库尚未收录此书"}), 200
        else:
            return jsonify({"error": "向量节点返回异常"}), res.status_code
            
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"无法连接到独立向量节点: {str(e)}"}), 500

@novel_ai_bp.route('/graph/trigger_sync', methods=['POST'])
def trigger_vector_sync():
    """通知向量节点开始下载小说并执行向量化计算"""
    try:
        res = requests.post(f"{VECTOR_NODE_URL}/api/vector/sync", timeout=3)
        return jsonify(res.json())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"指令下发失败: {str(e)}"}), 500
import threading
import time
import requests
import sqlite3
import json,re
import os  # 🌟 修复：补齐 os 模块导入
from flask import Blueprint, request, jsonify

# ================= 配置区 =================
novel_ai_bp = Blueprint('novel_ai', __name__, url_prefix='/api/novel')
RESOURCE_NODE_URL = "http://one4.zin6.dpdns.org:8100" 
DB_PATH = 'universal_data.db'

AI_MODEL = 'huihui_ai/qwen3.5-abliterated:9b'
AI_BASE_URL = "http://apple4.zin6.dpdns.org:11434/v1"

# ================= 全局状态与线程控制 =================
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

# ================= 核心分析与存储 =================
def save_to_db(collection, payload):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO store (collection, payload) VALUES (?, ?)', 
            (collection, json.dumps(payload, ensure_ascii=False))
        )

def get_analyzed_novels():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis'")
            rows = cursor.fetchall()
            return {json.loads(row[0]).get('novel_name') for row in rows if row[0]}
    except Exception:
        return set()
def analyze_core(novel_name, is_batch=False):
    import re
    
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

    system_prompt = "你是一个无情的正则化文本提取程序。严格遵守格式，严禁分点，严禁寒暄。"
    
    user_prompt = f"""
任务：提取小说《{novel_name}》的情报。

【强制输出模板】（只能输出这四行，严禁输出任何其他废话！）
内容简介：[在这里写100字以内的一段话概括]
完结状态：[已完结/连载中]
原因解释：[一句话原因]
分类标签：[3到5个核心关键词，用逗号分隔]

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

    print("\n" + "="*20 + f" [Ollama Request: {novel_name}] " + "="*20)
    print(f"URL: {ollama_url}")
    
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
        result_text = "内容简介：提取失败\n完结状态：未知\n原因解释：模型返回为空\n分类标签：未分类"

    # 🌟 终极解析器：暴力剥离干扰符，按关键字截取内容
    plain_text = result_text.replace('*', '') # 剥离所有可能扰乱正则的星号
    
    summary = "提取失败"
    status = "未知"
    reason = "未提供"
    tags_str = "未分类"

    # 使用 re.S 让点号匹配换行符，无视模型是否写成了一坨
    m_sum = re.search(r'内容简介\s*[:：]\s*(.*?)(?=完结状态|$)', plain_text, re.S)
    if m_sum: summary = m_sum.group(1).strip()

    m_stat = re.search(r'完结状态\s*[:：]\s*(.*?)(?=原因解释|$)', plain_text, re.S)
    if m_stat: status = m_stat.group(1).strip()

    m_reas = re.search(r'原因解释\s*[:：]\s*(.*?)(?=分类标签|$)', plain_text, re.S)
    if m_reas: reason = m_reas.group(1).strip()

    m_tags = re.search(r'分类标签\s*[:：]\s*(.*)', plain_text, re.S)
    if m_tags: tags_str = m_tags.group(1).strip()

    # 清洗并生成标签数组
    raw_tags = tags_str.replace('，', ',').replace('、', ',').replace(' ', ',').replace('\n', '').split(',')
    tags = [t.strip() for t in raw_tags if t.strip()]

    if "已完结" in status:
        tags.insert(0, "已完结")
    elif "连载中" in status:
        tags.insert(0, "连载中")
    if not tags:
        tags = ["未分类"]

    # 🌟 由 Python 强行拼装完美排版
    clean_result = (
        f"**内容简介**：{summary}\n"
        f"**完结状态**：{status}\n"
        f"**原因解释**：{reason}\n"
        f"**分类标签**：{', '.join(tags)}"
    )

    model_short_name = AI_MODEL.split('/')[-1]
    final_output = f"📊 **总字数**：约 {word_count:,} 字\n⏱️ **AI 耗时**：{elapsed_time}s | 🧠 **模型**：{model_short_name}\n\n{clean_result}"
    
    db_payload = {
        "novel_name": novel_name,
        "word_count": word_count,
        "analysis_result": final_output,
        "tags": tags,
        "raw_prompt": user_prompt,
        "raw_response": json.dumps(raw_json, ensure_ascii=False, indent=2)
    }
    save_to_db("novel_analysis", db_payload)
    
    return db_payload

# ================= 后台批量扫描线程 =================
def _run_batch_scan():
    global scan_state, start_time
    scan_state["is_running"] = True
    scan_state["status_msg"] = "正在获取小说列表..."
    scan_state["processed"] = 0
    scan_state["success_count"] = 0
    scan_state["recent_results"] = []
    scan_state["current_task"] = None
    start_time = time.time()

    try:
        res = requests.get(f"{RESOURCE_NODE_URL}/api/novels/json", timeout=10)
        if res.status_code != 200:
            raise Exception("无法连接资源节点获取列表")
        all_novels = res.json()
        
        scan_state["status_msg"] = "正在比对数据库缓存..."
        analyzed_set = get_analyzed_novels()
        pending_novels = [n for n in all_novels if n not in analyzed_set]
        
        scan_state["total"] = len(pending_novels)
        
        if not pending_novels:
            scan_state["status_msg"] = "所有小说已分析完毕"
            return

        for novel in pending_novels:
            if stop_event.is_set():
                scan_state["status_msg"] = "任务被手动中止"
                break
                
            scan_state["status_msg"] = f"正在投喂数据: {novel}"
            scan_state["total_time_sec"] = int(time.time() - start_time)
            
            try:
                result = analyze_core(novel, is_batch=True)
                scan_state["success_count"] += 1
                
                is_finished = "完结" in result['analysis_result'] or "大结局" in result['analysis_result']
                stream_item = {
                    "filename": novel,
                    "category": f"{result['word_count'] // 10000}万字",
                    "ai_tags": ["已完结" if is_finished else "连载中"]
                }
                scan_state["recent_results"].insert(0, stream_item)
                if len(scan_state["recent_results"]) > 5:
                    scan_state["recent_results"].pop()
                    
            except Exception as e:
                print(f"分析失败 {novel}: {str(e)}")
            
            scan_state["processed"] += 1
            
        if not stop_event.is_set():
            scan_state["status_msg"] = "批量分析完成"

    except Exception as e:
        scan_state["status_msg"] = f"异常停止: {str(e)}"
    finally:
        scan_state["is_running"] = False
        scan_state["current_task"] = None
        scan_state["total_time_sec"] = int(time.time() - start_time)

# ================= API 路由 =================

@novel_ai_bp.route('/list', methods=['GET'])
def list_novels():
    try:
        res = requests.get(f"{RESOURCE_NODE_URL}/api/novels/json", timeout=5)
        if res.status_code == 200:
            return jsonify({"items": [{"filename": name} for name in res.json()]})
        return jsonify({"items": []})
    except:
        return jsonify({"items": []})

# ================= 调试与重置扩展 =================

@novel_ai_bp.route('/reset', methods=['POST'])
def reset_database():
    """重置数据库分析记录"""
    target = request.json.get('novel_name')
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if target:
                conn.execute("DELETE FROM store WHERE collection='novel_analysis' AND payload LIKE ?", (f'%"{target}"%',))
                msg = f"已重置《{target}》的分析数据"
            else:
                conn.execute("DELETE FROM store WHERE collection='novel_analysis'")
                msg = "分析数据库已全量清空"
        
        scan_state["recent_results"] = []
        scan_state["success_count"] = 0
        scan_state["status_msg"] = msg
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@novel_ai_bp.route('/scan/status/debug', methods=['GET'])
def scan_status_debug():
    """返回比普通 status 更全的内部变量"""
    debug_info = scan_state.copy()
    debug_info["_server_time"] = time.time()
    debug_info["_thread_active"] = scan_thread.is_alive() if scan_thread else False
    debug_info["_db_path"] = os.path.abspath(DB_PATH)
    return jsonify(debug_info)

@novel_ai_bp.route('/content/<path:filename>', methods=['GET'])
def get_novel_content(filename):
    try:
        res = requests.get(f"{RESOURCE_NODE_URL}/api/novel/content/{filename}", timeout=10)
        return jsonify(res.json()) if res.status_code == 200 else jsonify({"error": "读取失败"}), res.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@novel_ai_bp.route('/scan', methods=['POST'])
def control_scan():
    global scan_thread, stop_event
    action = request.json.get('action')
    
    if action == 'start':
        if scan_state["is_running"]:
            return jsonify({"status": "error", "message": "扫描已在进行中"})
        stop_event.clear()
        scan_thread = threading.Thread(target=_run_batch_scan)
        scan_thread.daemon = True
        scan_thread.start()
        return jsonify({"status": "success", "message": "后台扫描已启动"})
        
    elif action == 'stop':
        if scan_state["is_running"]:
            stop_event.set()
            return jsonify({"status": "success", "message": "正在中止扫描..."})
        return jsonify({"status": "error", "message": "没有正在运行的扫描"})
        
    return jsonify({"error": "未知指令"}), 400

@novel_ai_bp.route('/scan/status', methods=['GET'])
def scan_status():
    if scan_state["is_running"]:
        scan_state["total_time_sec"] = int(time.time() - start_time)
    return jsonify(scan_state)

@novel_ai_bp.route('/analyze', methods=['POST'])
def analyze_novel_single():
    novel_name = request.json.get('novel_name')
    if not novel_name:
        return jsonify({"error": "缺少参数"}), 400
    try:
        result = analyze_core(novel_name, is_batch=False)
        return jsonify({"status": "success", "result": result["analysis_result"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =====================================================================
# ================= 手机阅读 App (Legado) 专用接口 =======================
# =====================================================================

text_cache = {}

def get_and_split_chapters(novel_name):
    """获取全文并在后端正则分章（带内存缓存，避免重复切分）"""
    if novel_name in text_cache:
        return text_cache[novel_name]
        
    res = requests.get(f"{RESOURCE_NODE_URL}/api/novel/content/{novel_name}", timeout=10)
    if res.status_code != 200:
        return []
        
    text = res.json().get('content', '')
    
    import re
    regex = re.compile(r'(?:^|\n)(第[零一二三四五六七八九十百千万\d]+[章回节卷部][^\n]*)')
    chapters = []
    last_idx = 0
    last_title = "引子 / 前言"
    
    for match in regex.finditer(text):
        content = text[last_idx:match.start()].strip()
        if len(content) > 20 or not chapters:
            chapters.append({"title": last_title, "content": content})
        last_title = match.group(1).strip()
        last_idx = match.end()
        
    chapters.append({"title": last_title, "content": text[last_idx:].strip()})
    
    if len(chapters) <= 1:
        chapters = [{"title": f"第 {i//5000 + 1} 节", "content": text[i:i+5000]} for i in range(0, len(text), 5000)]
        
    text_cache[novel_name] = chapters
    return chapters

@novel_ai_bp.route('/legado/search', methods=['GET'])
def legado_search():
    """阅读 App 搜索与发现接口"""
    keyword = request.args.get('key', '').lower()
    try:
        res = requests.get(f"{RESOURCE_NODE_URL}/api/novels/json", timeout=5)
        all_novels = res.json()
        
        # 优化：一次性取出所有 AI 分析数据，避免在循环中频繁查库
        analysis_dict = {}
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis'")
            for row in cursor.fetchall():
                data = json.loads(row[0])
                analysis_dict[data.get('novel_name')] = data

        results = []
        for name in all_novels:
            if keyword in name.lower() or not keyword:
                analysis = analysis_dict.get(name, {})
                # 过滤掉简介里的 Markdown 加粗符，适应手机端显示
                intro = analysis.get('analysis_result', '暂无 AI 简介').replace('**', '')
                tags = ",".join(analysis.get('tags', []))
                
                results.append({
                    "name": name.rsplit('.', 1)[0],
                    "author": "云端书库", 
                    "filename": name,
                    "intro": intro,
                    "kind": tags
                })
        return jsonify(results)
    except:
        return jsonify([])


import csv
import io
import re
from flask import make_response

@novel_ai_bp.route('/export/csv', methods=['GET'])
def export_analysis_csv():
    """导出分析结果：抹除 Markdown 干扰，精准正则提取"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload, create_time FROM store WHERE collection='novel_analysis' ORDER BY id DESC")
            rows = cursor.fetchall()

        count = len(rows)
        output = io.StringIO()
        output.write('\ufeff') # 防止 Excel 打开乱码
        writer = csv.writer(output)
        
        writer.writerow(['书名', '总字数', 'AI模型', '推理耗时(s)', '内容简介', '完结状态', '原因解释', '标签', '分析时间'])

        for row in rows:
            data = json.loads(row[0])
            res_text = data.get('analysis_result', '')
            
            # 1. 暴力剥离所有 Markdown 粗体符号，还原纯净文本
            plain_text = res_text.replace('**', '')
            
            # 2. 构造通用提取器 (re.S 允许跨行匹配)
            def get_val(pattern, text, default=""):
                m = re.search(pattern, text, re.S)
                return m.group(1).strip().replace('\n', ' ') if m else default

            # 3. 精准截断提取
            # 匹配 "AI 耗时：15.3s"
            elapsed = get_val(r'AI 耗时[：:]\s*([\d\.]+)s?', plain_text, "0")
            # 匹配 "模型：qwen3.5-abliterated:9b"
            model_name = get_val(r'模型[：:]\s*(.*?)(?:\n|$)', plain_text, "未知模型")
            
            # 利用下一个标题作为截断点，防止简介内容过长被腰斩
            summary = get_val(r'内容简介[：:]\s*(.*?)(?=\n完结状态|$)', plain_text)
            status = get_val(r'完结状态[：:]\s*(.*?)(?=\n原因解释|$)', plain_text)
            reason = get_val(r'原因解释[：:]\s*(.*?)(?=\n分类标签|$)', plain_text)
            
            tags_list = data.get('tags', [])

            writer.writerow([
                data.get('novel_name', '未知'),
                data.get('word_count', 0),
                model_name,
                elapsed,
                summary,
                status,
                reason,
                ", ".join(tags_list),
                row[1]
            ])

        response = make_response(output.getvalue())
        filename = f"novel_analysis_total_{count}.csv"
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        response.headers["Content-type"] = "text/csv; charset=utf-8"
        return response
    except Exception as e:
        return jsonify({"error": str(e)}), 500

from urllib.parse import quote
@novel_ai_bp.route('/analysis/<path:filename>', methods=['GET'])
def get_single_analysis(filename):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # 精确查询当前书籍的最新一条记录
        cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis' AND payload LIKE ? ORDER BY id DESC LIMIT 1", (f'%"{filename}"%',))
        row = cursor.fetchone()
        if row:
            return jsonify(json.loads(row[0]))
    return jsonify({"error": "Not found"}), 404

@novel_ai_bp.route('/download/<path:filename>', methods=['GET'])
def download_novel(filename):
    try:
        # 从资源节点获取内容
        res = requests.get(f"{RESOURCE_NODE_URL}/api/novel/content/{filename}", timeout=10)
        if res.status_code != 200:
            return "文件不存在", 404
            
        content = res.json().get('content', '')
        response = make_response(content)
        
        # 1. 处理文件名乱码：提取纯文件名并进行 URL 编码
        raw_name = filename.split('/')[-1]
        # 针对不同浏览器兼容性的标准写法
        encoded_name = quote(raw_name) 
        
        # 2. 设置响应头：使用 filename* 参数来显式声明 UTF-8 编码
        response.headers["Content-Disposition"] = f"attachment; filename={encoded_name}; filename*=UTF-8''{encoded_name}"
        
        # 3. 确保正文编码声明为 utf-8
        response.headers["Content-Type"] = "text/plain; charset=utf-8"
        
        return response
    except Exception as e:
        return str(e), 500


@novel_ai_bp.route('/legado/toc', methods=['GET'])
def legado_toc():
    """阅读 App 目录接口"""
    novel_name = request.args.get('file')
    chapters = get_and_split_chapters(novel_name)
    
    # 将 file 字段带入 JSON，方便正文接口调用
    toc_list = [{"name": ch["title"], "index": idx, "file": novel_name} for idx, ch in enumerate(chapters)]
    return jsonify(toc_list)

@novel_ai_bp.route('/legado/chapter', methods=['GET'])
def legado_chapter():
    """阅读 App 正文接口"""
    novel_name = request.args.get('file')
    idx = int(request.args.get('index', 0))
    
    chapters = get_and_split_chapters(novel_name)
    if 0 <= idx < len(chapters):
        return jsonify({"content": chapters[idx]["content"]})
    return jsonify({"content": "章节不存在或已越界"})

@novel_ai_bp.route('/legado/source', methods=['GET'])
def get_legado_source():
    """阅读 App 一键网络导入接口 (动态生成配置)"""
    # 自动获取当前访问的域名/IP和端口
    host_url = request.host_url.rstrip('/')
    # 提取用户请求时带入的访问码
    access_code = request.args.get('code', '8888')
    
    # 动态组装 JSON，将密码和 IP 自动注入到规则中
    # 注意：Python 的 f-string 中，阅读 App 的 {{key}} 语法需要写成 {{{{key}}}} 来转义
    source_config = [
      {
        "bookSourceGroup": "自建云端",
        "bookSourceName": "云端幻境智能书库(直连版)",
        "bookSourceType": "0",
        "bookSourceUrl": host_url,
        "customOrder": 0,
        "enable": True,
        "ruleBookAuthor": "$.author",
        "ruleBookContent": "$.content",
        "ruleBookIntro": "$.intro",
        "ruleBookKind": "$.kind",
        "ruleBookName": "$.name",
        "ruleChapterList": "$[*]",
        "ruleChapterName": "$.name",
        "ruleContentUrl": f"/api/novel/legado/chapter?file={{{{$.file}}}}&index={{{{$.index}}}}&code={access_code}",
        "ruleFindAuthor": "$.author",
        "ruleFindIntro": "$.intro",
        "ruleFindKind": "$.kind",
        "ruleFindList": "$[*]",
        "ruleFindName": "$.name",
        "ruleFindNoteUrl": f"/api/novel/legado/toc?file={{{{$.filename}}}}&code={access_code}",
        "ruleFindUrl": f"发现::/api/novel/legado/search?code={access_code}",
        "ruleSearchAuthor": "$.author",
        "ruleSearchIntro": "$.intro",
        "ruleSearchKind": "$.kind",
        "ruleSearchList": "$[*]",
        "ruleSearchName": "$.name",
        "ruleSearchNoteUrl": f"/api/novel/legado/toc?file={{{{$.filename}}}}&code={access_code}",
        "ruleSearchUrl": f"/api/novel/legado/search?key={{{{key}}}}&code={access_code}",
        "weight": 9999 # 权重设到最高，搜索时优先显示你的私有书库
      }
    ]
    return jsonify(source_config)

# ================= 网页端专属高速分页接口 =================

@novel_ai_bp.route('/toc/<path:filename>', methods=['GET'])
def web_toc(filename):
    """仅返回轻量级目录，极速加载详情页"""
    chapters = get_and_split_chapters(filename)
    if not chapters:
        return jsonify({"error": "解析失败"}), 404
        
    # 剥离正文，只传输标题和索引，将几 MB 的数据量压缩到几十 KB
    toc_list = [{"title": ch["title"], "index": idx} for idx, ch in enumerate(chapters)]
    return jsonify({"toc": toc_list, "total": len(chapters)})

@novel_ai_bp.route('/chapter/<path:filename>/<int:index>', methods=['GET'])
def web_chapter(filename, index):
    """精准返回单章正文，极速翻页"""
    chapters = get_and_split_chapters(filename)
    if 0 <= index < len(chapters):
        return jsonify({
            "title": chapters[index]["title"], 
            "content": chapters[index]["content"], 
            "total": len(chapters)
        })
    return jsonify({"error": "章节越界"}), 404
from collections import Counter

@novel_ai_bp.route('/tags/stats', methods=['GET'])
def get_tag_stats():
    """后端统计所有标签出现的频次"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis'")
            rows = cursor.fetchall()
            
        all_tags = []
        for row in rows:
            data = json.loads(row[0])
            all_tags.extend(data.get('tags', []))
            
        return jsonify(dict(Counter(all_tags)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@novel_ai_bp.route('/analysis/detail/<path:filename>', methods=['GET'])
def get_novel_analysis_detail(filename):
    """精准查询单本书的 AI 分析数据"""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # 查找包含该书名的最新记录
        cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis' AND payload LIKE ? ORDER BY id DESC LIMIT 1", (f'%"{filename}"%',))
        row = cursor.fetchone()
        if row:
            return jsonify(json.loads(row[0]))
    return jsonify({"error": "No analysis found"}), 404


@novel_ai_bp.route('/tags/stats', methods=['GET'])
def get_tag_stats():
    """获取全库标签统计，用于首页显示数量"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload FROM store WHERE collection='novel_analysis'")
            rows = cursor.fetchall()
            
        stats = {}
        for row in rows:
            data = json.loads(row[0])
            for tag in data.get('tags', []):
                stats[tag] = stats.get(tag, 0) + 1
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
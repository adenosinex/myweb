import threading
import time
import requests
import sqlite3
import json
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
    "current_task": None, # 🌟 新增：当前正在处理的上下文预览
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
    """核心大模型推理逻辑（强制锁死输出格式与长度）"""
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

    # 1. 系统提示词：剥夺思考自由，定义为格式化机器
    system_prompt = "你是一个无情的正则化文本提取程序。严禁进行发散性评价。必须且只能按照要求的模板输出，多一个字都不行。"
    
    # 2. 强硬约束与单样本示范 (One-Shot)
    user_prompt = f"""
任务：提取小说《{novel_name}》的情报。

【绝对禁令】
1. 严禁分点列出简介！简介必须是一小段连贯的纯文本（100字以内）。
2. 严禁生成“人物小传”、“看点亮点”、“剧情走向”等任何多余的标题和板块！
3. 严禁使用诸如“1. ”、“* ”等列表符号。

【强制输出模板】（只能输出这三行，禁止任何前言后语）
**内容简介**：[在这里写100字以内的一段话概括]
**完结状态**：[已完结/连载中]
**原因解释**：[一句话原因]

【正确示范】
**内容简介**：本书讲述了主角意外获得催眠异能后，在家庭内部利用该能力引发的一系列充满伦理张力与欲望纠葛的荒诞故事，文风直白且尺度极大。
**完结状态**：连载中
**原因解释**：结尾剧情仍停留在家庭关系的发展高潮，无明确收尾迹象。

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
            "temperature": 0.0,  # 降到绝对零度，抹杀所有发散性创造力
            "num_predict": 150,  # 物理防线：最多只允许生成 150 个 token（大约一百多汉字），强行切断长篇大论
            "num_ctx": 12000
        }
    }
    
    resp = requests.post(ollama_url, json=payload, timeout=120)
    resp.raise_for_status()
    result_text = resp.json().get("message", {}).get("content", "").strip()
    
    # 3. 最终清洗防线：如果模型还是发疯写了多余内容，强行截取前三行
    clean_lines = [line for line in result_text.split('\n') if line.strip() and not line.startswith('1.') and not line.startswith('*')]
    clean_result = '\n'.join(clean_lines[:3])
    
    final_output = f"📊 **总字数**：约 {word_count:,} 字\n\n{clean_result}"
    
    db_payload = {
        "novel_name": novel_name,
        "word_count": word_count,
        "analysis_result": final_output
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
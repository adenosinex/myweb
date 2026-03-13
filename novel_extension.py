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
    """核心大模型推理逻辑（解除行数截断，数据全量入库供前端调试）"""
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
            "num_predict": 500, # 调大 token 上限，防止话没说完被截断
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
    
    # 🌟 将原始 prompt 和响应一同存入数据库，传给前端
    db_payload = {
        "novel_name": novel_name,
        "word_count": word_count,
        "analysis_result": final_output,
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
    target = request.json.get('novel_name') # 如果传了名字则删单本，不传则全删
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if target:
                # 物理删除单本记录，payload 是 JSON，需要用 LIKE 匹配
                conn.execute("DELETE FROM store WHERE collection='novel_analysis' AND payload LIKE ?", (f'%"{target}"%',))
                msg = f"已重置《{target}》的分析数据"
            else:
                # 物理全量清空分析集合
                conn.execute("DELETE FROM store WHERE collection='novel_analysis'")
                msg = "分析数据库已全量清空"
        
        # 重置当前状态，让前端感知到变化
        scan_state["recent_results"] = []
        scan_state["success_count"] = 0
        scan_state["status_msg"] = msg
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# 状态轮询接口已存在，我们只需在返回前多塞点 debug 信息
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
import os
import json
import uuid
import threading
import requests
import time
import re
import traceback
from flask import Blueprint, request, jsonify, render_template, Response

ai_chapter_bp = Blueprint('ai_chapter_forge', __name__, template_folder='templates', url_prefix='/ai_chapter_forge')

UPLOAD_FOLDER = 'uploads'
CACHE_FOLDER = 'caches'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CACHE_FOLDER, exist_ok=True)

AI_BASE_URL = "http://apple4.zin6.dpdns.org:11434/v1"
AI_MODEL = 'huihui_ai/qwen3.5-abliterated:9b'
AI_MODEL = 'huihui_ai/qwen2.5-abliterate:3b-instruct'

def call_ollama(prompt):
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "think": False,
        "temperature": 0.1, # 调低温度，强制格式稳定性
        "options": {
            "num_ctx": 8192  # 强制分配 8K token 的上下文，避免溢出重算
        }
    }
    try:
        response = requests.post(f"{AI_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=600)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f"API_ERROR: {str(e)}"

def parse_llm_result(result_text, chapter_index):
    """双重解析：优先尝试 JSON 提取，失败后回退至正则表达式"""
    try:
        # 清理可能存在的 markdown 代码块包裹
        clean_text = result_text.replace('```json', '').replace('```', '')
        json_match = re.search(r'\{.*\}', clean_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
            return data.get('title', f"第{chapter_index+1}章"), data.get('summary', '无简介')
    except:
        pass

    # JSON 解析失败，回退正则
    title_match = re.search(r'标题\s*[:：]\s*(.*)', result_text)
    summary_match = re.search(r'简介\s*[:：]\s*(.*)', result_text, re.DOTALL)

    title = f"第{chapter_index+1}章 " + (title_match.group(1).replace('*', '').strip() if title_match else "未命名章节")
    summary = summary_match.group(1).replace('*', '').strip() if summary_match else result_text[:50] + "..."
    return title, summary

def get_file_content(filepath):
    """读取文件内容，处理编码"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        with open(filepath, 'r', encoding='gbk', errors='ignore') as f:
            return f.read()

def run_test_task(task_id, filepath):
    cache_path = os.path.join(CACHE_FOLDER, f"{task_id}.json")
    def update_log(msg):
        with open(cache_path, 'r+', encoding='utf-8') as f:
            state = json.load(f)
            state['overall_summary'] = msg
            f.seek(0); json.dump(state, f, ensure_ascii=False); f.truncate()

    try:
        update_log("正在解析本地文件内容...")
        content = get_file_content(filepath)
        
        update_log("正在提取前 500 字并构建 Prompt...")
        test_text = content[:500]

        # 增加 num_ctx 优化 M4 性能
        prompt = (
            f"你是一个文学编辑。请阅读以下小说片段，生成标题和简介。\n"
            f"必须严格按照以下 JSON 格式输出：\n"
            f'{{"title": "章节标题", "summary": "100字简介"}}\n\n'
            f"内容：\n{test_text}"
        )

        update_log("正在等待 Ollama 响应 (首次加载模型较慢)...")
        # 注意：这里我们增加了 options 参数
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "options": {"num_ctx": 4096, "num_thread": 8} 
        }
        
        response = requests.post(f"{AI_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=180)
        response.raise_for_status()
        result = response.json()['choices'][0]['message']['content'].strip()
        
        update_log("收到模型回复，正在解析格式...")
        title, summary = parse_llm_result(result, 0)

        with open(cache_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        state['status'] = "test_completed"
        state['test_result'] = {"title": title, "summary": summary}
        state['overall_summary'] = "测试完成，等待用户确认。"
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)

    

    except Exception as e:
        traceback.print_exc()
        with open(cache_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        state['status'] = "error"
        state['overall_summary'] = f"测试失败: {str(e)}"
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)

def run_full_task(task_id, filepath, chunk_size, mode):
    """正式处理线程"""
    cache_path = os.path.join(CACHE_FOLDER, f"{task_id}.json")
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            state = json.load(f)

        content = get_file_content(filepath)
        chunks = [content[i:i+chunk_size] for i in range(0, len(content), chunk_size)]
        
        state.update({
            "status": "processing",
            "total": len(chunks),
            "start_time": time.time(),
            "chapters": [{"raw_text": chunk, "title": "", "summary": "", "done": False} for chunk in chunks]
        })
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        
        for i, chapter in enumerate(state['chapters']):
            if chapter['done']:
                continue
            
            raw_text = chapter['raw_text']
            if mode == 'fast':
                analyze_text = raw_text[:800] + "\n...(中间略)...\n" + raw_text[-800:] if len(raw_text) > 1600 else raw_text
            else:
                analyze_text = raw_text

            prompt = (
                f"你是一个文学编辑。请阅读以下小说片段，生成标题和简介。\n"
                f"必须严格按照以下 JSON 格式输出，不要包含任何其他说明文字：\n"
                f"{{\n"
                f'  "title": "章节标题(不超过15字)",\n'
                f'  "summary": "内容简介(100字左右，概括剧情)"\n'
                f"}}\n\n"
                f"小说内容片段：\n{analyze_text}"
            )

            result = call_ollama(prompt)
            title, summary = parse_llm_result(result, i)

            state['chapters'][i]['title'] = title
            state['chapters'][i]['summary'] = summary
            state['chapters'][i]['done'] = True
            state['current'] = i + 1

            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)

        state['status'] = "completed"
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)

    except Exception as e:
        traceback.print_exc()
        if os.path.exists(cache_path):
            with open(cache_path, 'r', encoding='utf-8') as f:
                err_state = json.load(f)
            err_state['status'] = "error"
            err_state['overall_summary'] = f"发生错误: {str(e)}"
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(err_state, f, ensure_ascii=False)

# --- 路由接口 ---

@ai_chapter_bp.route('/ui')
def index_page():
    return render_template('novel_forge_index.html')

@ai_chapter_bp.route('/api/v1/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file"}), 400
    
    file = request.files['file']
    chunk_size = int(request.form.get('chunk_size', 5000))
    mode = request.form.get('mode', 'fast')
    
    task_id = str(uuid.uuid4())
    filepath = os.path.join(UPLOAD_FOLDER, f"{task_id}.txt")
    file.save(filepath)

    cache_path = os.path.join(CACHE_FOLDER, f"{task_id}.json")
    initial_state = {
        "status": "testing",
        "total": 0,
        "current": 0,
        "chunk_size": chunk_size,
        "mode": mode,
        "chapters": [],
        "test_result": None
    }
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(initial_state, f, ensure_ascii=False)

    # 启动测试线程
    t = threading.Thread(target=run_test_task, args=(task_id, filepath))
    t.start()

    return jsonify({"task_id": task_id})

@ai_chapter_bp.route('/api/v1/confirm_process/<task_id>', methods=['POST'])
def confirm_process(task_id):
    """前端确认测试结果后，触发正式处理"""
    cache_path = os.path.join(CACHE_FOLDER, f"{task_id}.json")
    if not os.path.exists(cache_path):
        return jsonify({"error": "Task not found"}), 404
        
    with open(cache_path, 'r', encoding='utf-8') as f:
        state = json.load(f)
    
    filepath = os.path.join(UPLOAD_FOLDER, f"{task_id}.txt")
    chunk_size = state.get('chunk_size', 5000)
    mode = state.get('mode', 'fast')

    # 启动完整处理线程
    t = threading.Thread(target=run_full_task, args=(task_id, filepath, chunk_size, mode))
    t.start()

    return jsonify({"status": "processing_started"})

@ai_chapter_bp.route('/api/v1/progress/<task_id>')
def get_progress(task_id):
    cache_path = os.path.join(CACHE_FOLDER, f"{task_id}.json")
    if not os.path.exists(cache_path):
        return jsonify({"status": "not_found"}), 404
    
    with open(cache_path, 'r', encoding='utf-8') as f:
        state = json.load(f)
        
    return jsonify({
        "status": state.get('status'),
        "total": state.get('total', 0),
        "current": state.get('current', 0),
        "start_time": state.get('start_time'),
        "test_result": state.get('test_result'),
        "overall_summary": state.get('overall_summary'),
        "chapters": [{"title": c['title'], "summary": c['summary'], "done": c['done']} for c in state.get('chapters', [])]
    })

@ai_chapter_bp.route('/api/v1/download/<task_id>')
def download_result(task_id):
    cache_path = os.path.join(CACHE_FOLDER, f"{task_id}.json")
    if not os.path.exists(cache_path):
        return "Not Found", 404

    with open(cache_path, 'r', encoding='utf-8') as f:
        state = json.load(f)

    def generate():
        for ch in state.get('chapters', []):
            if ch['done']:
                # 仅导出已处理完成的章节：包含标题、简介和原文片段
                yield f"\n\n### {ch['title']} ###\n"
                yield f"【本章简介】{ch['summary']}\n\n"
                yield ch.get('raw_text', '')
            # 移除了 else 分支，后续未处理的章节不再写入文件

    filename = f"forge_{task_id[:8]}.txt"
    return Response(generate(), mimetype='text/plain', headers={"Content-Disposition": f"attachment;filename={filename}"})
    cache_path = os.path.join(CACHE_FOLDER, f"{task_id}.json")
    if not os.path.exists(cache_path):
        return "Not Found", 404

    with open(cache_path, 'r', encoding='utf-8') as f:
        state = json.load(f)

    def generate():
        for i, ch in enumerate(state.get('chapters', [])):
            if ch['done']:
                yield f"\n\n### {ch['title']} ###\n"
                yield f"【本章简介】{ch['summary']}\n\n"
            else:
                yield f"\n\n### (第{i+1}章 待处理) ###\n\n"
            yield ch.get('raw_text', '')

    filename = f"forge_{task_id[:8]}.txt"
    return Response(generate(), mimetype='text/plain', headers={"Content-Disposition": f"attachment;filename={filename}"})
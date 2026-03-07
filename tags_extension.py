# tags_extension.py
import sqlite3
import json
import csv
import io
import os
import re
import requests
from flask import Blueprint, request, jsonify, make_response

tags_bp = Blueprint('tags', __name__)
DB_PATH = 'universal_data.db'

# ================= 配置管理模块 =================
# 全局标签列表：约束 AI 的输出边界，防止标签库被污染
TAG_CHOICES = [
    "深夜emo", "健身", "说唱", "图书馆", "通勤", "驾车",
    "起床", "DJ", "助眠", "抖音漫游", "Chill", "洗澡",
    "快乐", "电音", "音乐视频", "春节", "粤语", "失恋",
    "躺平", "欧美", "打扫", "国风", "会员", "游戏",
    "专注", "沉浸", "夜晚", "治愈", "轻音乐", "小酒馆",
    "KTV", "情歌", "摇滚", "R&B", "佛系", "怀旧",
    "民谣", "女声", "K-pop", "日语", "旅行", "摸鱼",
    "儿歌", "雨天", "海边", "乡村", "古典", "学习"
]

@tags_bp.route('/api/tags', methods=['GET'])
def get_tags():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # 确保表存在，防止初次运行报错
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS song_tags (
                song_name TEXT PRIMARY KEY,
                tags TEXT
            )
        ''')
        cursor.execute('SELECT song_name, tags FROM song_tags')
        rows = cursor.fetchall()
    
    tags_dict = {}
    all_categories = set()
    for row in rows:
        try:
            tags = json.loads(row[1])
            tags_dict[row[0]] = tags
            all_categories.update(tags)
        except json.JSONDecodeError:
            tags_dict[row[0]] = []
            
    return jsonify({
        "song_tags": tags_dict,
        "categories": list(all_categories)
    })

@tags_bp.route('/api/tags', methods=['POST'])
def save_tags():
    data = request.json
    song_name = data.get('song_name')
    tags = data.get('tags', [])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR REPLACE INTO song_tags (song_name, tags) VALUES (?, ?)',
                     (song_name, json.dumps(tags, ensure_ascii=False)))
    return jsonify({"status": "success"})

@tags_bp.route('/api/tags', methods=['DELETE'])
def delete_tags():
    song_name = request.json.get('song_name')
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM song_tags WHERE song_name=?', (song_name,))
    return jsonify({"status": "success"})

@tags_bp.route('/api/tags/csv', methods=['GET'])
def download_tags_csv():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT song_name, tags FROM song_tags')
        rows = cursor.fetchall()
    
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['歌曲名', '分类标签'])
    for row in rows:
        tags_str = " / ".join(json.loads(row[1]))
        cw.writerow([row[0], tags_str])
        
    output = make_response(si.getvalue().encode('utf-8-sig'))
    output.headers["Content-Disposition"] = "attachment; filename=song_tags_export.csv"
    output.headers["Content-type"] = "text/csv; charset=utf-8"
    return output

@tags_bp.route('/api/ai/tag', methods=['POST'])
def ai_tag():
    data = request.json
    song_name = data.get('song_name')
    
    # 支持前端传入或环境变量兜底
    api_key = data.get('api_key') or os.environ.get('SILICONFLOW_API_KEY') or os.environ.get('SILICON_API_KEY')
    model_name = data.get('model_name') or os.environ.get('SILICON_MODEL', 'Qwen/Qwen2.5-72B-Instruct')
    
    if not api_key:
        return jsonify({"error": "缺少 API Key。请在页面填写，或在后端配置 SILICONFLOW_API_KEY 环境变量"}), 400
        
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    tags_str = "，".join(TAG_CHOICES)
    
    # 优化 Prompt：加入 Few-Shot 示例，明确要求输出格式
    prompt = (
        f"歌曲名：'{song_name}'\n"
        f"请根据这首歌曲的常见受众和氛围，从以下列表中选择1到3个最符合的标签。\n\n"
        f"可选标签列表：[{tags_str}]\n\n"
        f"严格按照以下格式输出：\n标签：[标签1, 标签2]\n\n"
        f"要求：\n1. 只能从列表中选择，绝不要自己编造新标签。\n"
        f"2. 除了标签列表外，不要输出任何分析、解释或多余的废话。\n"
        f"示例1：\n歌曲名：'海底'\n标签：[深夜emo, 助眠]\n"
        f"示例2：\n歌曲名：'好运来'\n标签：[快乐, 派对]"
    )
    
    payload = {
        "model": model_name, 
        "messages": [
            {"role": "system", "content": "你是一个专业的音乐标签分类助手，严格遵循用户的格式要求。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    try:
        resp = requests.post("https://api.siliconflow.cn/v1/chat/completions", json=payload, headers=headers, timeout=15)
        
        # 1. 拦截非 JSON 返回值（比如网关挂了返回 502 HTML）
        try:
            res_json = resp.json()
        except Exception:
            return jsonify({"error": f"API 返回非 JSON 数据: {resp.text}"}), 500
            
        # 2. 拦截返回了 JSON 字符串而非字典的极端情况
        if not isinstance(res_json, dict):
            return jsonify({"error": f"API 拒绝请求，返回文本: {res_json}"}), 500

        # 3. 拦截 API 明确的错误响应（余额不足、Key 错误等）
        if 'choices' not in res_json:
            # 兼容 OpenAI 标准的 {"error": {"message": "..."}} 格式
            err_data = res_json.get('error', {})
            if isinstance(err_data, dict):
                error_msg = err_data.get('message', str(res_json))
            else:
                error_msg = str(err_data)
                
            # 如果依然找不到，尝试拿外层的 message
            if error_msg == str(res_json):
                error_msg = res_json.get('message', str(res_json))
                
            return jsonify({"error": f"API 服务端拒绝: {error_msg}"}), 500
            
        # 4. 正常解析标签
        raw_text = res_json['choices'][0]['message']['content'].strip()
        raw_text = raw_text.replace('\n', ' ').replace('，', ',')
        
        match = re.search(r'标签[:：]\s*(.*)', raw_text)
        if match:
            tags_result = match.group(1).strip()
        else:
            tags_result = raw_text 
            
        tags_result = tags_result.strip('[]【】')
        extracted_tags = [t.strip() for t in tags_result.split(',') if t.strip()]
        
        # 后处理防幻觉：强制剔除不在 TAG_CHOICES 中的标签
        valid_tags = [t for t in extracted_tags if t in TAG_CHOICES]
        
        if not valid_tags:
            return jsonify({"tags": ["未分类"]})
            
        return jsonify({"tags": valid_tags[:3]}) 
        
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"网络请求异常: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"后端处理异常: {str(e)}"}), 500